"""2.3 bar customization model (ui/bar_config.py) — pure data, no Qt.

Run offscreen:  QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest tests/test_bar_config.py
"""
import unittest

from tide.ui import bar_config as bc


class NormalizeTests(unittest.TestCase):
    def test_empty_is_the_shipped_bar(self) -> None:
        cfg = bc.normalize({})
        self.assertEqual(cfg, bc.defaults())
        self.assertEqual(cfg["order"], list(bc.DEFAULT_ORDER))
        self.assertTrue(bc.is_default({}))
        self.assertTrue(bc.is_default(None))

    def test_garbage_never_raises(self) -> None:
        for raw in (None, [], "x", 3, {"order": "nope", "hidden": 5,
                                        "sizes": [], "art": 7, "line1": "t"}):
            with self.subTest(raw=raw):
                self.assertEqual(bc.normalize(raw), bc.defaults())

    def test_unknown_ids_drop_and_missing_ids_return_home(self) -> None:
        cfg = bc.normalize({"order": ["volume", "bogus", "play", "|", "play"]})
        order = cfg["order"]
        self.assertNotIn("bogus", order)
        self.assertEqual(order.count("play"), 1)
        self.assertEqual(sorted(x for x in order if x != bc.GAP), sorted(bc.ITEMS))
        gap = order.index(bc.GAP)
        # a re-added transport id lands left of the gap, a tool right of it
        self.assertLess(order.index("shuffle"), gap)
        self.assertGreater(order.index("sleep"), gap)

    def test_lost_gap_comes_back(self) -> None:
        cfg = bc.normalize({"order": ["play", "next", "volume"]})
        self.assertIn(bc.GAP, cfg["order"])

    def test_play_can_never_hide(self) -> None:
        self.assertNotIn("play", bc.normalize({"hidden": ["play"]})["hidden"])
        self.assertNotIn("play", bc.set_shown({}, "play", False)["hidden"])

    def test_medium_size_is_not_stored(self) -> None:
        cfg = bc.set_size({}, "play", "l")
        self.assertEqual(cfg["sizes"], {"play": "l"})
        self.assertEqual(bc.set_size(cfg, "play", "m")["sizes"], {})
        self.assertEqual(bc.size_factor(cfg, "play"), bc.SIZE_FACTORS["l"])

    def test_bad_enums_fall_back(self) -> None:
        cfg = bc.normalize({"next": "sideways", "art": "xl", "time": "?",
                            "density": "fluffy"})
        d = bc.defaults()
        for k in ("next", "art", "time", "density"):
            self.assertEqual(cfg[k], d[k])

    def test_normalize_returns_a_fresh_object(self) -> None:
        a = bc.normalize({})
        a["order"].append("x")
        self.assertNotIn("x", bc.normalize({})["order"])


class VerbTests(unittest.TestCase):
    def test_move_reorders(self) -> None:
        cfg = bc.move({}, "volume", 0)
        self.assertEqual(cfg["order"][0], "volume")
        cfg = bc.move(cfg, bc.GAP, 0)
        left, right = bc.clusters(cfg)
        self.assertEqual(left, [])
        self.assertIn("play", right)

    def test_hidden_drop_from_visible_order(self) -> None:
        cfg = bc.set_shown({}, "repeat", False)
        self.assertNotIn("repeat", bc.visible_order(cfg))
        self.assertIn("repeat", cfg["order"])     # keeps its slot
        self.assertNotIn("repeat", bc.set_shown(cfg, "repeat", True)["hidden"])

    def test_lines_default_to_the_variant(self) -> None:
        for variant, lines in bc.VARIANT_LINES.items():
            with self.subTest(variant=variant):
                self.assertEqual(bc.lines_for({}, variant), lines)
        cfg = bc.with_change({}, line1=["title", "bogus", "title"])
        l1, l2 = bc.lines_for(cfg, "headline")
        self.assertEqual(l1, ("title",))
        self.assertEqual(l2, bc.VARIANT_LINES["headline"][1])

    def test_empty_line_is_kept_distinct_from_style_own(self) -> None:
        cfg = bc.with_change({}, line2=[])
        self.assertEqual(bc.lines_for(cfg, "stacked")[1], ())

    def test_time_formats(self) -> None:
        self.assertEqual(bc.format_time("both", 42, 190), "0:42 / 3:10")
        self.assertEqual(bc.format_time("elapsed", 42, 190), "0:42")
        self.assertEqual(bc.format_time("remaining", 42, 190), "−2:28")
        self.assertEqual(bc.format_time("off", 42, 190), "")


if __name__ == "__main__":
    unittest.main()
