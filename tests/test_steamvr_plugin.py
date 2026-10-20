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
import time
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

    def test_ripple_caps_full_flashes_at_three_a_second(self):
        r = self.ring.Ripple()
        starts = full = 0
        for i in range(90):                             # 8 kicks a second for 1 s
            t = i / 90
            before = r.started
            r.update(t, 1.0 if (i % 11) < 2 else 0.0)
            if r.started != before:
                starts += 1
                full += r.strength >= 1.0
        self.assertLessEqual(full, 3)
        self.assertEqual(starts, 9)                     # every kick still shows, softer

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


class PokeTests(unittest.TestCase):
    """a fingertip on the panel (2026-10-19: poking it did nothing)."""

    W, H = 0.42, 0.42 * 640 / 1000

    def run_path(self, zs, x=0.0, y=0.0):
        poke = geo.Poke()
        out = []
        for z in zs:
            out += [k for k, _u, _v in poke.update(np.array([x, y, z]), self.W, self.H)]
        return poke, out

    def test_press_drag_release(self):
        poke, kinds = self.run_path([0.05, 0.03, 0.01, 0.004, 0.002, 0.003, 0.02])
        self.assertIn("down", kinds)
        self.assertEqual(kinds.count("down"), 1)
        self.assertEqual(kinds[-1], "up")
        self.assertFalse(poke.pressed)

    def test_shaky_finger_doesnt_double_click(self):
        # wobbling a few mm around the surface stays one press
        _poke, kinds = self.run_path([0.03, 0.005, 0.009, 0.004, 0.012, 0.003, 0.03])
        self.assertEqual(kinds.count("down"), 1)
        self.assertEqual(kinds.count("up"), 1)

    def test_no_press_from_behind(self):
        _poke, kinds = self.run_path([-0.03, -0.01, 0.0, 0.004])
        self.assertNotIn("down", kinds)

    def test_through_the_panel_lets_go(self):
        _poke, kinds = self.run_path([0.02, 0.004, -0.02, -0.07])
        self.assertEqual(kinds[-1], "up")

    def test_outside_does_nothing(self):
        _poke, kinds = self.run_path([0.02, 0.004], x=0.5)
        self.assertEqual(kinds, [])

    def test_coordinates(self):
        poke = geo.Poke()
        poke.update(np.array([0.0, 0.0, 0.02]), self.W, self.H)
        events = poke.update(np.array([-self.W / 2, self.H / 2, 0.004]), self.W, self.H)
        _k, u, v = events[-1]
        self.assertAlmostEqual(u, 0.0)
        self.assertAlmostEqual(v, 0.0)          # top left


def _seethrough():
    import types
    pkg = types.ModuleType("tide_plugins_see_steamvr")
    pkg.__path__ = [str(BUNDLED_DIR / "steamvr")]
    sys.modules["tide_plugins_see_steamvr"] = pkg
    return importlib.import_module("tide_plugins_see_steamvr.seethrough")


def _facing_head(z=-0.5):
    """a panel at head height ``z`` out, its face toward the head."""
    return at(0.0, 1.7, z)


class ControllerTipTests(unittest.TestCase):
    """touch comes from the front of the controller, not a fingertip 11 cm
    past it (2026-10-19)."""

    def test_tip_is_on_the_controller(self):
        ctrl = at(0.2, 1.2, -0.3)
        for side in ("left", "right"):
            tip = geo.controller_tip(ctrl, side)
            self.assertLess(np.linalg.norm(tip - ctrl[:3, 3]), 0.03)

    def test_hands_mirror(self):
        ctrl = at(0.0, 0.0, 0.0)
        left, right = geo.controller_tip(ctrl, "left"), geo.controller_tip(ctrl, "right")
        self.assertAlmostEqual(left[0], -right[0])
        self.assertAlmostEqual(left[1], right[1])


class TouchModeTests(unittest.TestCase):
    """laser or touch (2026-10-19: "laser doesn't work anymore")."""

    W, H = 0.42, 0.42 * 640 / 1000

    def test_hands_held_out_keep_the_laser(self):
        # pointing from 25-40 cm away used to switch the laser off for good
        mode = geo.TouchMode()
        for z in (0.40, 0.30, 0.25, 0.2, 0.3):
            self.assertFalse(mode.update([np.array([0.05, -0.05, z])], self.W, self.H, False))

    def test_a_tip_at_the_panel_switches_to_touch_and_back(self):
        mode = geo.TouchMode()
        self.assertTrue(mode.update([np.array([0.0, 0.0, 0.05])], self.W, self.H, False))
        self.assertTrue(mode.update([np.array([0.0, 0.0, 0.12])], self.W, self.H, False))
        self.assertFalse(mode.update([np.array([0.0, 0.0, 0.2])], self.W, self.H, False))

    def test_a_press_holds_touch(self):
        mode = geo.TouchMode()
        mode.update([np.array([0.0, 0.0, 0.02])], self.W, self.H, False)
        self.assertTrue(mode.update([np.array([0.0, 0.0, 0.3])], self.W, self.H, True))

    def test_off_to_the_side_is_laser(self):
        mode = geo.TouchMode()
        self.assertFalse(mode.update([np.array([0.5, 0.0, 0.02])], self.W, self.H, False))


class SeeThroughTests(unittest.TestCase):
    """the panel thins out over a controller in front of it (2026-10-19:
    "i can't see my controllers in front of the menu")."""

    PX = (1000, 640)
    W = 0.42

    def setUp(self):
        self.see = _seethrough()

    def test_through_panel_scales_toward_the_eye(self):
        panel = _facing_head()
        x, y, r, front = geo.through_panel(panel, np.array([0.0, 1.7, 0.0]),
                                           np.array([0.05, 1.7, -0.25]), 0.05)
        self.assertTrue(front)
        self.assertAlmostEqual(x, 0.1)        # halfway to the eye: twice as far out
        self.assertAlmostEqual(r, 0.1)

    def test_behind_lands_where_it_is(self):
        panel = _facing_head()
        x, _y, r, front = geo.through_panel(panel, np.array([0.0, 1.7, 0.0]),
                                            np.array([0.05, 1.7, -0.6]), 0.05)
        self.assertFalse(front)
        self.assertAlmostEqual(x, 0.05)
        self.assertAlmostEqual(r, 0.05)

    def test_a_controller_in_front_makes_a_window(self):
        panel = _facing_head()
        hands = {"right": at(0.0, 1.7, -0.35), "left": None}
        caps = self.see.holes(panel, np.array([0.0, 1.7, 0.0]), hands, self.W, self.PX)
        self.assertEqual(len(caps), 1)
        keep = self.see.mask(caps, self.W, self.PX)
        g = self.see.GRID
        self.assertAlmostEqual(float(keep[320 // g, 500 // g]), self.see.FLOOR, places=2)
        self.assertAlmostEqual(float(keep[0, 0]), 1.0)

    def test_behind_the_panel_or_off_it_makes_none(self):
        panel = _facing_head()
        eye = np.array([0.0, 1.7, 0.0])
        self.assertEqual(self.see.holes(panel, eye, {"right": at(0.0, 1.7, -0.8)}, self.W, self.PX), [])
        self.assertEqual(self.see.holes(panel, eye, {"right": at(0.0, 0.9, -0.3)}, self.W, self.PX), [])

    def test_cursor_shrinks_as_you_close_in(self):
        far = self.see.cursor({"right": np.array([0.0, 0.0, 0.09])}, self.W, self.PX, None)
        near = self.see.cursor({"right": np.array([0.0, 0.0, 0.01])}, self.W, self.PX, None)
        self.assertLess(far[2], near[2])
        self.assertAlmostEqual(near[0], 500)
        self.assertIsNone(self.see.cursor({"right": np.array([0.0, 0.0, 0.3])}, self.W, self.PX, None))
        pressed = self.see.cursor({"right": np.array([0.0, 0.0, -0.02])}, self.W, self.PX, "right")
        self.assertTrue(pressed[3])

    def test_compose_thins_the_panel_and_leaves_the_frame_alone(self):
        import time
        from PySide6.QtGui import QColor, QGuiApplication, QImage
        _app = QGuiApplication.instance() or QGuiApplication([])
        base = QImage(*self.PX, QImage.Format_RGBA8888)
        base.fill(QColor(20, 20, 20, 255))
        caps = [(500.0, 320.0, 120.0, 520.0, 600.0, 140.0)]
        t0 = time.perf_counter()
        out = self.see.compose(base, caps, (100.0, 100.0, 0.5, False), self.W, "#ffd400")
        took = time.perf_counter() - t0
        self.assertLess(QColor.fromRgba(out.pixel(500, 320)).alpha(), 80)
        self.assertEqual(QColor.fromRgba(out.pixel(900, 50)).alpha(), 255)
        self.assertEqual(QColor.fromRgba(base.pixel(500, 320)).alpha(), 255)
        # the ring of the cursor is drawn ~18 px out from its middle
        ring = QColor.fromRgba(out.pixel(100 + 17, 100))
        self.assertGreater(ring.red(), 100)
        self.assertLess(took, 0.02)

    def test_same_skips_tiny_moves(self):
        a = [(1.0, 2.0, 3.0, 4.0, 5.0, 6.0)]
        self.assertTrue(self.see.same(a, [(1.5, 2.0, 3.0, 4.0, 5.0, 6.0)]))
        self.assertFalse(self.see.same(a, [(9.0, 2.0, 3.0, 4.0, 5.0, 6.0)]))
        self.assertFalse(self.see.same(a, []))
        self.assertTrue(self.see.same(None, None))
        self.assertFalse(self.see.same(None, (1.0, 2.0, 0.5, False)))


class SpringTests(unittest.TestCase):
    def test_overshoots_then_lands(self):
        values = [geo.ease_out_back(i / 50) for i in range(51)]
        self.assertGreater(max(values), 1.0)
        self.assertAlmostEqual(values[-1], 1.0)
        self.assertAlmostEqual(values[0], 0.0)


class GuardTests(unittest.TestCase):
    """one bad tick no longer ends the session (2026-10-19 disconnects)."""

    def test_a_failing_tick_is_noted_once_and_skipped(self):
        import types
        pkg = types.ModuleType("tide_plugins_guard_steamvr")
        pkg.__path__ = [str(BUNDLED_DIR / "steamvr")]
        sys.modules["tide_plugins_guard_steamvr"] = pkg
        try:
            vr = importlib.import_module("tide_plugins_guard_steamvr.vr")
            log_path = Path(tempfile.mkdtemp()) / "sessions.log"
            t = vr.VrThread(vr.VrBridge(), None, {"log_path": str(log_path)})
            t._errors_seen = set()

            def boom(_now):
                raise ValueError("bad pose")
            for _ in range(5):
                t._guard("watch", boom, 0.0)          # no raise
            self.assertEqual(log_path.read_text().count("watch tick failed"), 1)

            def lost(_now):
                raise vr._Lost("steamvr quit")
            with self.assertRaises(vr._Lost):
                t._guard("watch", lost, 0.0)
        finally:
            for name in [n for n in sys.modules if n.startswith("tide_plugins_guard_steamvr")]:
                del sys.modules[name]


class WhisperTests(unittest.TestCase):
    def test_sound_notes_are_not_words(self):
        from tide import speech_whisper as sw
        self.assertEqual(sw.clean(" [BLANK_AUDIO]"), "")
        self.assertEqual(sw.clean("(music) Surreal."), "surreal")
        self.assertEqual(sw.clean("♪ Like Him ♪"), "like him")
        self.assertEqual(sw.clean("Tyler, the Creator"), "tyler, the creator")

    def test_features_have_whispers_shape(self):
        from tide import speech_whisper as sw
        mel = sw.log_mel(np.zeros(16000, dtype=np.float32))
        self.assertEqual(mel.shape, (80, 3000))
        filters = sw.mel_filters()
        self.assertEqual(filters.shape, (80, 201))
        self.assertTrue((filters.sum(axis=1) > 0).all())

    def test_pinned_files(self):
        from tide import speech_whisper as sw
        self.assertEqual(len(sw.FILES), 4)
        self.assertEqual(round(sw.total_bytes() / 1e6), 78)
        for _name, size, sha in sw.FILES.values():
            self.assertEqual(len(sha), 64)

    def test_hears_surreal(self):
        """the word wav2vec2 got wrong. needs the model and espeak-ng."""
        import shutil
        import subprocess
        import wave
        from tide import speech_whisper as sw
        if not sw.available() or shutil.which("espeak-ng") is None or shutil.which("ffmpeg") is None:
            self.skipTest("no whisper model or espeak-ng here")
        d = Path(tempfile.mkdtemp())
        subprocess.run(["espeak-ng", "-v", "en-us", "-s", "150", "-w", str(d / "a.wav"), "surreal"],
                       check=True, capture_output=True)
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(d / "a.wav"), "-ar", "16000",
                        "-ac", "1", str(d / "b.wav")], check=True)
        w = wave.open(str(d / "b.wav"))
        audio = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
        self.assertEqual(sw.transcribe(audio), "surreal")


class RingFeelTests(unittest.TestCase):
    """2026-10-19: the ring sat on the very edge of the lens, out of sight,
    and its pulse stepped at 60 Hz on a 144 Hz headset."""

    def setUp(self):
        import types
        pkg = types.ModuleType("tide_plugins_ringfeel_steamvr")
        pkg.__path__ = [str(BUNDLED_DIR / "steamvr")]
        sys.modules["tide_plugins_ringfeel_steamvr"] = pkg
        self.ring = importlib.import_module("tide_plugins_ringfeel_steamvr.ring")

    def test_envelope_hits_then_glides_down(self):
        env = self.ring.Envelope()
        self.assertEqual(env.update(0.0, 0.9), 0.9)               # straight up, like the app
        falling = [env.update(i / 144, 0.1) for i in range(1, 6)]
        self.assertTrue(all(b < a for a, b in zip(falling, falling[1:])))
        self.assertGreater(falling[0], 0.5)                       # a glide, not a drop
        self.assertLess(env.update(0.2, 0.1), 0.12)               # gone well before the next beat

    @staticmethod
    def beat_stream(beats, seconds):
        """tide's pulse at 60 Hz: each beat jumps to its height and falls
        away, on top of whatever is still ringing."""
        out = []
        for i in range(int(seconds * 60)):
            t = i / 60
            level = 0.0
            for bt, h in beats:
                if bt <= t:
                    level = max(level, h * math.exp(-(t - bt) / 0.09))
            out.append((t, level))
        return out

    def count_kicks(self, stream, seconds, jitter=True):
        """run the stream through the vr side like vr.py does: pulses land
        in uneven batches between 144 Hz frames, the frame keeps the peak."""
        import random
        rng = random.Random(5)
        ripple = self.ring.Ripple()
        kicks, soft = 0, 0
        last_start = ripple.started
        i = 0
        for f in range(int(seconds * 144)):
            now = f / 144
            peak = None
            # a busy gui thread delivers late, in clumps
            while i < len(stream) and stream[i][0] <= now - (rng.uniform(0, 0.03) if jitter else 0):
                peak = stream[i][1] if peak is None else max(peak, stream[i][1])
                i += 1
            if peak is None:
                peak = stream[i - 1][1] if i else 0.0
            ripple.update(now, peak)
            if ripple.started != last_start:
                last_start = ripple.started
                kicks += 1
                soft += ripple.strength < 1.0
        return kicks, soft

    def test_every_beat_the_app_shows_ripples(self):
        # soft kicks (0.35), a fast roll that never dips under 0.4, loud hits
        beats = [(0.2, 0.9), (0.7, 0.35), (1.2, 0.8), (1.7, 0.4)]
        beats += [(2.0 + k / 6, 0.75) for k in range(12)]          # 6 a second
        beats += [(4.3, 1.0), (4.8, 0.5)]
        kicks, soft = self.count_kicks(self.beat_stream(beats, 5.2), 5.2)
        self.assertEqual(kicks, len(beats))
        # never more than three full flashes a second: the roll's extra hits are soft
        self.assertGreater(soft, 0)

    def test_a_long_note_is_one_kick(self):
        stream = [(i / 60, 0.0 if i < 30 else min(0.9, (i - 30) * 0.08)) for i in range(180)]
        kicks, _soft = self.count_kicks(stream, 3.0, jitter=False)
        self.assertEqual(kicks, 1)

    def test_glow_peaks_inside_the_lens_edge(self):
        from PySide6.QtGui import QGuiApplication
        _app = QGuiApplication.instance() or QGuiApplication([])
        eye = {"uv": [], "proj": [-1.2, 1.2, -1.2, 1.2], "eye_x": 0.0}
        images, _w = self.ring.draw({"left": dict(eye), "right": dict(eye)})
        glow = images[0]
        h = glow.height()
        y = h // 2
        row = [glow.pixelColor(x, y).alpha() for x in range(glow.width() // 2)]
        peak = max(range(len(row)), key=row.__getitem__)
        first = next(x for x, a in enumerate(row) if a > 0)
        # the brightest part sits clearly in from where the lens starts
        self.assertGreater(peak - first, glow.width() // 2 * 0.04)


def _vr_pkg(name):
    import types
    pkg = types.ModuleType(name)
    pkg.__path__ = [str(BUNDLED_DIR / "steamvr")]
    sys.modules[name] = pkg
    return pkg


class _FakeOverlay:
    def __init__(self):
        self.interactive = True
        self.visible = False
        self.has_frame = True
        self.a = 1.0
        self.w = 0.0

    def set_interactive(self, on):
        self.interactive = on

    def show(self, on):
        self.visible = on

    def alpha(self, a):
        self.a = a

    def width(self, w):
        self.w = w

    def place(self, _pose):
        pass


class VrThreadPanelTests(unittest.TestCase):
    """laser only, the back of the panel, its glow (2026-10-19)."""

    def setUp(self):
        _vr_pkg("tide_plugins_vrpanel_steamvr")
        self.vr = importlib.import_module("tide_plugins_vrpanel_steamvr.vr")
        self.geo = importlib.import_module("tide_plugins_vrpanel_steamvr.geometry")
        self.events = []
        bridge = self.vr.VrBridge()
        bridge.panel_mouse.connect(lambda k, x, y: self.events.append(k))
        t = self.vr.VrThread(bridge, None, {"log_path": os.devnull})
        t.panel, t.back, t.glow = _FakeOverlay(), _FakeOverlay(), _FakeOverlay()
        t.panel.visible = True
        t.input = None
        t.pokes = {s: self.geo.Poke() for s in self.vr.SIDES}
        t.poke_px = (-1.0, -1.0)
        t.touch = self.geo.TouchMode()
        t.see_cursor = None
        t.behind = False
        t.last_touch = 0.0
        t.glow_flash = self.geo.FlashLimiter()
        t.glow_env = importlib.import_module("tide_plugins_vrpanel_steamvr.ring").Envelope()
        t.panel_pose = self.geo.spawn_in_front(at(0.0, 1.7, 0.0))
        self.t = t

    def approach(self):
        for i, z in enumerate((0.12, 0.06, 0.03, 0.01, -0.003, 0.03, 0.2)):
            ctrl = self.t.panel_pose @ at(0.0, 0.0, z - 0.02)
            self.t._poke(float(i), {"right": ctrl, "left": None}, 0.42)
            yield self.t.panel.interactive

    def test_touch_presses(self):
        list(self.approach())
        self.assertIn("down", self.events)

    def test_laser_only_never_lets_go_of_the_laser(self):
        self.t.settings["touch"] = False
        self.assertTrue(all(self.approach()))
        self.assertNotIn("down", self.events)

    def test_from_behind_only_the_back_shows(self):
        self.t.behind = True
        self.t._panel_look(0.0, 0.8, 0.42)
        self.assertEqual(self.t.panel.a, 0.0)
        self.assertTrue(self.t.back.visible)
        self.assertAlmostEqual(self.t.back.a, 0.8)
        self.t.behind = False
        self.t._panel_look(0.1, 0.8, 0.42)
        self.assertAlmostEqual(self.t.panel.a, 0.8)
        self.assertFalse(self.t.back.visible)

    def test_no_touching_from_behind(self):
        self.t.behind = True
        list(self.approach())
        self.assertNotIn("down", self.events)
        self.assertFalse(self.t.panel.interactive)     # no laser through the back either

    def test_glow_beats_and_switches_off(self):
        self.t.settings.update({"panel_glow": "kick", "ring_strength": "loud"})
        self.t.playing = True
        self.t.beat = 1.0
        level = 0.0
        for i in range(30):
            level = self.t._glow_level(i / 144)
        self.assertGreater(level, 0.3)
        self.t.settings["panel_glow"] = "off"
        self.assertEqual(self.t._glow_level(1.0), 0.0)

    def test_turn_around_faces_back(self):
        pose = self.t.panel_pose @ self.geo.turn_around()
        self.assertTrue(np.allclose(pose[:3, 2], -self.t.panel_pose[:3, 2]))
        self.assertTrue(np.allclose(pose[:3, 3], self.t.panel_pose[:3, 3]))


class PanelLookTests(unittest.TestCase):
    """fonts from tide's type roles, the lyric fade, the back and the glow."""

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

    def setUp(self):
        from PySide6.QtWidgets import QApplication
        from tide.plugin_host import PluginHost
        from tide.ui import typefaces
        from tests.test_plugins import FakePlayer, FakeQueue
        self.app = QApplication.instance() or QApplication([])
        self.typefaces = typefaces
        self.saved_roles = typefaces.roles()
        typefaces.set_roles({"title": {"family": "DejaVu Serif", "weight": 700, "size": 115},
                             "numbers": {"family": "DejaVu Sans Mono"}})
        tmp = Path(tempfile.mkdtemp(prefix="tide-vr-"))
        self.player = FakePlayer()
        self.host = PluginHost(None, self.player, FakeQueue(), roots=[(BUNDLED_DIR, "bundled")],
                               state_file=tmp / "p.toml", settings_dir=tmp / "s",
                               stamp=tmp / "stamp", data_dir=tmp / "d")
        self.assertTrue(self.host.set_enabled("steamvr", True))
        self.plugin = self.host.record("steamvr").instance
        for _ in range(10):
            self.app.processEvents()

    def tearDown(self):
        self.host.set_enabled("steamvr", False)
        self.typefaces.set_roles(self.saved_roles)

    def test_fonts_follow_the_type_roles(self):
        now = self.plugin.panel.now
        self.assertEqual(now.title.font().family(), "DejaVu Serif")
        self.assertEqual(now.title.font().pixelSize(), round(38 * 1.15))
        self.assertEqual(now.elapsed.font().family(), "DejaVu Sans Mono")
        body = self.plugin.ctx.theme.fonts()["ui"]["family"]
        self.assertTrue(body)
        self.assertEqual(now.meta.font().family(), body)

    def test_speed_changes_reach_the_panel(self):
        calls = []
        self.plugin.panel.refresh = lambda *a: calls.append(a)
        self.player.speed_changed.emit(1.25)
        self.app.processEvents()
        self.assertTrue(calls)

    def test_faded_lyric_stays_in_its_place(self):
        from PySide6.QtCore import QPoint
        panel = self.plugin.panel
        panel.show_tab("now")
        lyric = panel.now.lyric_now
        lyric.setText("WWWW the sung line WWWW")
        lyric.set_opacity(0.5)
        self.app.processEvents()
        img = panel.frame()
        top = lyric.mapTo(panel, QPoint(0, 0))
        inside = [img.pixelColor(top.x() + x, top.y() + lyric.height() // 2)
                  for x in range(0, 200, 2)]
        self.assertTrue(any(c.lightness() > 60 for c in inside))

    def test_back_and_glow(self):
        from tide_plugins.steamvr import panel as pm, ring
        back = pm.back_face(self.plugin.ctx)
        self.assertEqual((back.width(), back.height()), (1000, 640))
        self.assertEqual(back.pixelColor(500, 600).alpha(), 255)
        glow, ratio = ring.panel_glow((1000, 640), 26, ((1, 0.4, 0.6), (0.3, 0.9, 0.5)))
        self.assertGreater(ratio, 1.1)
        cx, cy = glow.width() // 2, glow.height() // 2
        self.assertEqual(glow.pixelColor(cx, cy).alpha(), 0)          # nothing under the panel
        edge = int((glow.width() - 1000) / 2)
        self.assertGreater(glow.pixelColor(edge - 6, cy).alpha(), 120)  # bright at its edge
        self.assertLess(glow.pixelColor(4, cy).alpha(), 40)              # gone by the margin


class ToastTests(unittest.TestCase):
    """the now playing toast: a little under your gaze, lazy to follow,
    a few seconds long, never in the way (2026-10-20)."""

    def test_spot_sits_under_your_gaze_not_the_horizon(self):
        level = geo.toast_spot(HEAD)
        self.assertLess(level[1], 1.7)
        self.assertAlmostEqual(float(np.linalg.norm(level - HEAD[:3, 3])), geo.TOAST_DISTANCE_M)
        # look 50° down: it's still under where you look, well below the eye line
        c, s = math.cos(math.radians(-50)), math.sin(math.radians(-50))
        down = HEAD.copy()
        down[:3, :3] = np.array([[1, 0, 0], [0, c, -s], [0, s, c]])
        self.assertLess(geo.toast_spot(down)[1], level[1] - 0.5)

    def test_lazy_follow_ignores_a_glance_then_catches_up(self):
        f = geo.LazyFollow()
        f.update(0.0, np.zeros(3))
        f.update(0.1, np.array([0.1, 0.0, 0.0]))       # a glance: stays put
        self.assertTrue(np.allclose(f.pos, 0.0))
        target = np.array([0.6, 0.0, 0.0])               # turned away: it follows
        steps = [f.update(0.1 + i / 90, target)[0] for i in range(1, 200)]
        self.assertTrue(all(b >= a for a, b in zip(steps, steps[1:])))   # smooth, no jumps back
        self.assertGreater(steps[-1], 0.57)
        self.assertLess(steps[3], 0.2)                    # a glide, not a snap

    def test_fade(self):
        self.assertEqual(geo.toast_alpha(0.0, 4.0), 0.0)
        self.assertEqual(geo.toast_alpha(2.0, 4.0), 1.0)
        self.assertGreater(geo.toast_alpha(4.5, 4.0), 0.0)
        self.assertEqual(geo.toast_alpha(6.0, 4.0), 0.0)


class VrToastTests(unittest.TestCase):
    def setUp(self):
        _vr_pkg("tide_plugins_vrtoast_steamvr")
        self.vr = importlib.import_module("tide_plugins_vrtoast_steamvr.vr")
        g = importlib.import_module("tide_plugins_vrtoast_steamvr.geometry")
        t = self.vr.VrThread(self.vr.VrBridge(), None, {"log_path": os.devnull, "toast": "follow"})
        t.toast = _FakeOverlay()
        t.toast.place = lambda pose: setattr(t.toast, "pose", pose)
        t.panel_open = False
        t.toast_follow = g.LazyFollow()
        self.head = at(0.0, 1.7, 0.0)
        t._hmd = lambda: self.head
        self.t = t

    def test_comes_up_follows_and_goes(self):
        t = self.t
        t.toast_at = 0.0
        t._toast_tick(0.5)
        self.assertTrue(t.toast.visible)
        first = t.toast.pose[:3, 3].copy()
        # turn round 90°: it trails after you
        self.head = at(0.0, 1.7, 0.0)
        self.head[:3, :3] = np.array([[0, 0, 1], [0, 1, 0], [-1, 0, 0]], float)
        for i in range(1, 120):
            t._toast_tick(0.5 + i / 90)
        moved = t.toast.pose[:3, 3]
        self.assertGreater(float(np.linalg.norm(moved - first)), 1.0)
        t._toast_tick(10.0)
        self.assertFalse(t.toast.visible)
        self.assertIsNone(t.toast_at)

    def test_stays_put_when_asked(self):
        t = self.t
        t.settings["toast"] = "stay"
        t.toast_at = 0.0
        t._toast_tick(0.5)
        first = t.toast.pose[:3, 3].copy()
        self.head = at(1.0, 1.7, 0.0)
        for i in range(1, 60):
            t._toast_tick(0.5 + i / 90)
        self.assertTrue(np.allclose(t.toast.pose[:3, 3], first))

    def test_the_panel_opening_ends_it(self):
        t = self.t
        t.toast_at = 0.0
        t._toast_tick(0.5)
        t.panel_open = True
        t._toast_tick(0.6)
        self.assertFalse(t.toast.visible)
        self.assertIsNone(t.toast_at)


class ToastPluginTests(unittest.TestCase):
    """the plugin side: a toast only for the song you land on."""

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

    def test_skipping_fast_shows_only_where_you_land(self):
        import types
        from PySide6.QtWidgets import QApplication
        from tide.plugin_host import PluginHost
        from tests.test_plugins import FakePlayer, FakeQueue
        app = QApplication.instance() or QApplication([])
        tmp = Path(tempfile.mkdtemp(prefix="tide-vr-"))
        (tmp / "s").mkdir()
        (tmp / "s" / "steamvr.toml").write_text('toast = "follow"\nlyrics = false\n')
        player, queue = FakePlayer(), FakeQueue()
        player.state = "playing"
        host = PluginHost(None, player, queue, roots=[(BUNDLED_DIR, "bundled")],
                          state_file=tmp / "p.toml", settings_dir=tmp / "s",
                          stamp=tmp / "stamp", data_dir=tmp / "d")
        try:
            self.assertTrue(host.set_enabled("steamvr", True))
            plugin = host.record("steamvr").instance
            posted = []
            real = plugin.thread.post
            plugin.thread.post = lambda *cmd: (posted.append(cmd[0]), real(*cmd))
            plugin._toast_wait.setInterval(60)
            for n in ("one", "two", "three"):
                queue.current = types.SimpleNamespace(video_id=n, title=n, artists="a", album="",
                                                      duration_seconds=1, thumbnail="", source="ytmusic")
                plugin._track_changed(host.current_track())
                app.processEvents()
            t0 = time.monotonic()
            while time.monotonic() - t0 < 0.4:
                app.processEvents()
                time.sleep(0.01)
            self.assertEqual(posted.count("toast"), 1)
        finally:
            host.set_enabled("steamvr", False)
            host.shutdown()
