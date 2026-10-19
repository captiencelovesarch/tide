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
from PySide6.QtCore import Qt

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


def rot_y(deg):
    a = np.eye(4)
    r = math.radians(deg)
    a[0, 0], a[0, 2], a[2, 0], a[2, 2] = math.cos(r), math.sin(r), -math.sin(r), math.cos(r)
    return a


class WatchRuleTests(unittest.TestCase):
    """h3vr: aiming brought the watch up and froze the game."""

    def place_wrist(self, rotation):
        # put the controller so its wrist point sits in view, 35 cm out
        target = np.array([0.0, 1.55, -0.35])
        c = rotation.copy()
        off = (c[:3, :3] @ geo.WRIST_LEFT)
        c[:3, 3] = target - off - np.array([0.0, geo.WATCH_LIFT_M, 0.0])
        return c

    def test_aiming_doesnt_count(self):
        aiming = self.place_wrist(np.eye(4))          # pointing where you look
        self.assertLess(geo.aim_deg(HEAD, aiming), 10)
        self.assertFalse(geo.checking_watch(HEAD, aiming, "left"))

    def test_forearm_across_you_counts(self):
        across = self.place_wrist(rot_y(-90))         # pointing to your right
        self.assertGreater(geo.aim_deg(HEAD, across), 80)
        self.assertTrue(geo.checking_watch(HEAD, across, "left"))


class DwellTests(unittest.TestCase):
    def test_fills_in_a_second_and_drains_faster(self):
        d = geo.Dwell(1.0)
        t = 0.0
        while d.update(t, True) < 1.0:
            t += 1 / 90
        self.assertAlmostEqual(t, 1.0, delta=0.03)
        d2 = geo.Dwell(1.0)
        for i in range(46):
            d2.update(i / 90, True)                  # half full
        for i in range(46, 70):
            d2.update(i / 90, False)
        self.assertEqual(d2.value, 0.0)


class FlashLimiterTests(unittest.TestCase):
    def test_at_most_three_flashes_a_second(self):
        lim = geo.FlashLimiter()
        flashes, prev = 0, 0.0
        for i in range(90):                           # 1 s of 8 hits a second at 90 hz
            t = i / 90
            target = 1.0 if (i % 11) < 2 else 0.0
            lvl = lim.update(t, target)
            if lvl - prev >= 0.3:
                flashes += 1
            prev = lvl
        self.assertLessEqual(flashes, 3)

    def test_slow_music_passes_untouched(self):
        lim = geo.FlashLimiter()
        out = [lim.update(i / 90, 1.0 if i % 45 == 0 else 0.0) for i in range(90)]
        self.assertEqual(out.count(1.0), 2)


class RingTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("steamvr_ring", BUNDLED_DIR / "steamvr" / "ring.py")
        self.ring = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.ring)

    def test_levels(self):
        lv = self.ring.level
        self.assertEqual(lv("off", 1.0, True), 0.0)
        self.assertAlmostEqual(lv("kick", 0.0, True), 0.12)
        self.assertGreater(lv("kick", 0.8, True), lv("kick", 0.0, True))
        self.assertEqual(lv("breathing", 0.0, True), 0.3)
        self.assertEqual(lv("breathing", 1.0, False), 0.0)     # paused: nothing

    def test_draws_a_glow_and_a_rim(self):
        from PySide6.QtWidgets import QApplication
        QApplication.instance() or QApplication([])
        eye = {"uv": self.ring.ellipse_uv(), "proj": [-1.4, 1.2, -1.4, 1.4], "eye_x": -0.03}
        images, width = self.ring.draw({"left": eye, "right": dict(eye, eye_x=0.03)})
        self.assertEqual(len(images), 2)
        glow = images[0]
        self.assertEqual(glow.width(), 2 * self.ring.HALF_W)
        self.assertLessEqual(glow.height(), self.ring.MAX_TEX_H)
        self.assertGreater(width, 4.0)
        # bright at the edge of the view, clear in the middle of each eye
        h = glow.height()
        mid_left = glow.pixelColor(self.ring.HALF_W // 2, h // 2).alpha()
        self.assertLess(mid_left, 8)
        edge = max(glow.pixelColor(x, h // 2).alpha() for x in range(0, self.ring.HALF_W // 4))
        self.assertGreater(edge, 150)

    def test_smoothing_rounds_corners(self):
        square = [(0, 0), (1, 0), (1, 1), (0, 1)]
        out = self.ring.smooth(square, 2)
        self.assertEqual(len(out), 16)
        self.assertNotIn((0, 0), out)

    def test_ripple_starts_on_a_kick_and_fades_inward(self):
        r = self.ring.Ripple()
        alpha, scale = r.update(0.0, 0.1)
        self.assertEqual((alpha, scale), (0.0, 1.0))
        alpha, scale = r.update(0.01, 0.9)              # a kick
        self.assertGreater(alpha, 0.9)
        alpha2, scale2 = r.update(0.3, 0.9)             # held: no new ripple
        self.assertLess(alpha2, alpha)
        self.assertLess(scale2, 1.0)

    def test_ripple_caps_at_three_a_second(self):
        r = self.ring.Ripple()
        starts = 0
        for i in range(90):                             # 8 kicks a second for 1 s
            t = i / 90
            before = r.started
            r.update(t, 1.0 if (i % 11) < 2 else 0.0)
            starts += r.started != before
        self.assertLessEqual(starts, 3)

    def test_cover_colors(self):
        from PySide6.QtGui import QColor, QImage
        from PySide6.QtWidgets import QApplication
        QApplication.instance() or QApplication([])
        img = QImage(32, 32, QImage.Format_RGB32)
        img.fill(QColor("#808080"))
        self.assertIsNone(self.ring.cover_colors(img))  # grey: use the theme
        for y in range(16):
            for x in range(32):
                img.setPixelColor(x, y, QColor("#ff2266"))
        a, b = self.ring.cover_colors(img)
        self.assertGreater(a[0], 0.8)


class ForwarderTests(unittest.TestCase):
    """laser → qt: a drag in a list scrolls it and never clicks."""

    def setUp(self):
        from PySide6.QtWidgets import (QApplication, QPushButton, QScrollArea, QVBoxLayout,
                                       QWidget)
        self.app = QApplication.instance() or QApplication([])
        spec = importlib.util.spec_from_file_location(
            "tide_plugins_test_steamvr.panel", BUNDLED_DIR / "steamvr" / "panel.py",
            submodule_search_locations=None)
        # panel.py imports its siblings relatively: load it as part of a package
        import types
        pkg = types.ModuleType("tide_plugins_test_steamvr")
        pkg.__path__ = [str(BUNDLED_DIR / "steamvr")]
        sys.modules["tide_plugins_test_steamvr"] = pkg
        self.panel = importlib.import_module("tide_plugins_test_steamvr.panel")
        self.root = QWidget()
        self.root.setAttribute(Qt.WA_DontShowOnScreen, True)
        self.root.resize(400, 300)
        area = QScrollArea(self.root)
        area.setGeometry(0, 0, 400, 300)
        content = QWidget()
        col = QVBoxLayout(content)
        self.clicks = []
        for i in range(20):
            b = QPushButton(f"row {i}")
            b.setFixedHeight(60)
            b.clicked.connect(lambda _c=False, i=i: self.clicks.append(i))
            col.addWidget(b)
        area.setWidget(content)
        area.setWidgetResizable(True)
        self.area = area
        self.root.show()
        self.app.processEvents()

    def tearDown(self):
        import shiboken6
        shiboken6.delete(self.root)
        for name in [n for n in sys.modules if n.startswith("tide_plugins_test_steamvr")]:
            del sys.modules[name]

    def test_tap_clicks(self):
        fwd = self.panel.Forwarder(self.root)
        fwd.handle("move", 100, 30)
        fwd.handle("down", 100, 30)
        fwd.handle("up", 100, 30)
        self.assertEqual(self.clicks, [0])

    def test_drag_scrolls_without_clicking(self):
        fwd = self.panel.Forwarder(self.root)
        fwd.handle("move", 100, 250)
        fwd.handle("down", 100, 250)
        for y in range(250, 90, -10):
            fwd.handle("move", 100, y)
        fwd.handle("up", 100, 90)
        self.assertEqual(self.clicks, [])
        self.assertGreater(self.area.verticalScrollBar().value(), 100)

    def test_stick_scrolls(self):
        fwd = self.panel.Forwarder(self.root)
        fwd.handle("move", 100, 100)
        fwd.scroll(-2.0)
        self.assertEqual(self.area.verticalScrollBar().value(), 2 * fwd.STICK_PX)


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
        self.assertEqual((img.width(), img.height()), (1000, 640))
        for tab in ("search", "library", "settings", "now"):
            plugin.panel.show_tab(tab)
            app.processEvents()
        img = plugin.panel.frame()
        if plugin.ctx.theme.aesthetic() == "modern":
            # rounded corners are see-through, not black (2026-10-19)
            self.assertEqual(img.pixelColor(0, 0).alpha(), 0)
        self.assertEqual(img.pixelColor(500, 320).alpha(), 255)
        host.set_enabled("steamvr", False)
        # stopped, and every qt object it adopted is gone right away (not
        # left for python's exit, where deleting them crashed)
        import shiboken6
        for obj in (plugin.thread, plugin.panel, plugin.surface, plugin.bridge):
            self.assertFalse(shiboken6.isValid(obj))
        self.assertNotIn("tide_plugins.steamvr.vr", sys.modules)


if __name__ == "__main__":
    unittest.main()
