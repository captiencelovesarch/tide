"""Phase-1 registries: glyphs, backdrop styles, the speed law.

Three "as data" registries extracted for tide 2.0's personality presets.
None of them has migrated call sites yet, so what these tests pin is the
transcription: the default glyph pack must be unicode-identical to what
the windows hand-type today, the backdrop registry must equal exactly
what central_bg's renderer accepts, and speed_law's clamp must behave
byte-for-byte like the old ui/speed.py _clamp (which now re-exports it).

Run offscreen:  QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest tests/
"""
import sys
import unittest

from PySide6.QtWidgets import QApplication

from tide import backdrops, glyphs, speed_law


def _app() -> QApplication:
    return QApplication.instance() or QApplication(sys.argv[:1])


# ---------------------------------------------------------------------------
# glyphs
# ---------------------------------------------------------------------------

# The shipped vocabulary, spelled as escapes so a lookalike codepoint
# sneaking into the registry (⏸ for ▮▮, ▹ for ▸ …) can't pass by looking
# right in a diff.
SHIPPED_GLYPHS = {
    "play": "\u25b6",                       # ▶
    "pause": "\u25ae\u25ae",               # ▮▮ — NOT U+23F8, baseline war story
    "loading": "\u2026",                    # …
    "prev": "\u25c2\u25c2",                # ◂◂
    "next": "\u25b8\u25b8",                # ▸▸
    "shuffle": "\u21cb",                    # ⇋
    "repeat": "\u21bb",                     # ↻
    "repeat_one": "\u21bb\u00b9",          # ↻¹
    "like_on": "\u2665",                    # ♥
    "like_off": "\u2661",                   # ♡
    "sleep": "zzz",
    "fullscreen": "\u2922",                 # ⤢
    "heading_dash": "\u2500",               # ─
}


class GlyphRegistryTest(unittest.TestCase):
    def setUp(self) -> None:
        glyphs.set_pack("default")
        glyphs.set_overrides({})

    def tearDown(self) -> None:
        glyphs.set_pack("default")
        glyphs.set_overrides({})

    def test_default_pack_is_verbatim_transcription(self) -> None:
        self.assertEqual(glyphs.DEFAULT_PACK, SHIPPED_GLYPHS)
        for key, expected in SHIPPED_GLYPHS.items():
            self.assertEqual(glyphs.glyph(key), expected)

    def test_keys_cover_the_contract_vocabulary(self) -> None:
        self.assertEqual(set(glyphs.KEYS), set(SHIPPED_GLYPHS))
        # Ordered tuple, no dupes.
        self.assertEqual(len(glyphs.KEYS), len(set(glyphs.KEYS)))

    def test_every_registered_pack_is_complete(self) -> None:
        for name in glyphs.packs():
            glyphs.set_pack(name)
            for key in glyphs.KEYS:
                self.assertIsInstance(glyphs.glyph(key), str)

    def test_register_pack_rejects_missing_keys(self) -> None:
        partial = dict(SHIPPED_GLYPHS)
        del partial["repeat_one"]
        with self.assertRaises(ValueError):
            glyphs.register_pack("holey", partial)
        self.assertNotIn("holey", glyphs.packs())

    def test_pack_switching(self) -> None:
        alt = {k: f"[{k}]" for k in glyphs.KEYS}
        glyphs.register_pack("blocky", alt)
        glyphs.set_pack("blocky")
        self.assertEqual(glyphs.active_pack(), "blocky")
        self.assertEqual(glyphs.glyph("play"), "[play]")
        glyphs.set_pack("default")
        self.assertEqual(glyphs.glyph("play"), "▶")

    def test_set_pack_unknown_raises(self) -> None:
        with self.assertRaises(KeyError):
            glyphs.set_pack("nope")
        self.assertEqual(glyphs.active_pack(), "default")

    def test_registering_a_pack_copies_the_mapping(self) -> None:
        src = {k: f"[{k}]" for k in glyphs.KEYS}
        glyphs.register_pack("copied", src)
        src["play"] = "mutated"
        glyphs.set_pack("copied")
        self.assertEqual(glyphs.glyph("play"), "[play]")

    def test_overrides_win_over_the_active_pack(self) -> None:
        glyphs.set_overrides({"play": "▷"})
        self.assertEqual(glyphs.glyph("play"), "▷")
        self.assertEqual(glyphs.glyph("pause"), "▮▮")
        glyphs.set_overrides({})
        self.assertEqual(glyphs.glyph("play"), "▶")

    def test_overrides_drop_unknown_keys_silently(self) -> None:
        # Persisted user state from another version must never crash.
        glyphs.set_overrides({"play": "P", "from_the_future": "?"})
        self.assertEqual(glyphs.glyph("play"), "P")
        with self.assertRaises(KeyError):
            glyphs.glyph("from_the_future")

    def test_unknown_key_raises(self) -> None:
        with self.assertRaises(KeyError):
            glyphs.glyph("warp_ten")


# ---------------------------------------------------------------------------
# backdrops
# ---------------------------------------------------------------------------


class BackdropRegistryTest(unittest.TestCase):
    def test_registry_equals_central_bg_accepted_set(self) -> None:
        # central_bg's renderer knows three families: the two original
        # gradient looks, the liquid cover, and the _FX_STYLES scenes.
        # Their union IS the set_style whitelist — pin the registry to it.
        from tide.ui import central_bg
        self.assertEqual(
            set(backdrops.SLUGS),
            set(central_bg._FX_STYLES) | {"field", "band", "liquid"},
        )

    def test_set_style_accepts_every_slug(self) -> None:
        _app()
        from PySide6.QtWidgets import QWidget
        from tide.ui.central_bg import CentralBg
        w = CentralBg(QWidget())
        for slug in backdrops.SLUGS:
            w.set_style(slug)
            self.assertEqual(w._style, slug, f"renderer refused {slug!r}")

    def test_set_style_rejects_everything_else(self) -> None:
        _app()
        from PySide6.QtWidgets import QWidget
        from tide.ui.central_bg import CentralBg
        for junk in ("nope", "FIELD", "", "follow", "off"):
            w = CentralBg(QWidget())
            w.set_style("band")
            w.set_style(junk)
            self.assertEqual(w._style, "field", f"renderer accepted {junk!r}")

    def test_labels_cover_slugs_exactly(self) -> None:
        self.assertEqual(set(backdrops.LABELS), set(backdrops.SLUGS))
        for slug, label in backdrops.LABELS.items():
            self.assertTrue(label, f"empty label for {slug!r}")

    def test_choices_order_and_shape(self) -> None:
        rows = backdrops.choices()
        self.assertEqual([slug for slug, _ in rows], list(backdrops.SLUGS))
        self.assertEqual(
            rows,
            [(slug, backdrops.LABELS[slug]) for slug in backdrops.SLUGS],
        )

    def test_choices_with_follow_prepends(self) -> None:
        rows = backdrops.choices_with_follow()
        self.assertEqual(rows[0], (backdrops.FOLLOW, backdrops.FOLLOW_LABEL))
        self.assertEqual(rows[1:], backdrops.choices())

    def test_choices_with_off_appends(self) -> None:
        rows = backdrops.choices_with_off()
        self.assertEqual(rows[-1], (backdrops.OFF, backdrops.OFF_LABEL))
        self.assertEqual(rows[:-1], backdrops.choices())

    def test_sentinels_are_not_styles(self) -> None:
        self.assertNotIn(backdrops.FOLLOW, backdrops.SLUGS)
        self.assertNotIn(backdrops.OFF, backdrops.SLUGS)


# ---------------------------------------------------------------------------
# speed law
# ---------------------------------------------------------------------------


def _old_clamp(value: float) -> float:
    """The pre-move ui/speed.py _clamp, verbatim — the reference law."""
    snapped = round(float(value) / 0.05) * 0.05
    return max(0.5, min(2.0, round(snapped, 2)))


class SpeedLawTest(unittest.TestCase):
    def test_constants(self) -> None:
        self.assertEqual(speed_law.SPEED_PRESETS, [0.5, 0.75, 1.0, 1.25, 1.5, 2.0])
        self.assertEqual(speed_law.SPEED_MIN, 0.5)
        self.assertEqual(speed_law.SPEED_MAX, 2.0)
        self.assertEqual(speed_law.SPEED_STEP, 0.05)
        self.assertEqual(speed_law.ENGINE_MIN, 0.25)
        self.assertEqual(speed_law.ENGINE_MAX, 4.0)

    def test_clamp_identical_to_old_law_across_the_grid(self) -> None:
        # Sweep well past both ends at an increment that lands on-grid,
        # off-grid and on the .025 rounding boundary.
        for i in range(-100, 701):
            v = i * 0.007 + 0.0003
            self.assertEqual(speed_law.clamp(v), _old_clamp(v), f"v={v}")
        for p in speed_law.SPEED_PRESETS:
            self.assertEqual(speed_law.clamp(p), p)

    def test_clamp_quantizes_no_drift(self) -> None:
        # A chain of −0.05 nudges must land exactly, not at 1.0500000004×.
        v = 1.0
        for _ in range(10):
            v = speed_law.clamp(v - speed_law.SPEED_STEP)
        self.assertEqual(v, 0.5)
        self.assertEqual(speed_law.clamp(1.024), 1.0)
        self.assertEqual(speed_law.clamp(1.026), 1.05)

    def test_clamp_bounds(self) -> None:
        self.assertEqual(speed_law.clamp(0.1), 0.5)
        self.assertEqual(speed_law.clamp(3.7), 2.0)

    def test_engine_clamp(self) -> None:
        self.assertEqual(speed_law.engine_clamp(0.1), 0.25)
        self.assertEqual(speed_law.engine_clamp(10.0), 4.0)
        # No quantization inside the range.
        self.assertEqual(speed_law.engine_clamp(1.37), 1.37)
        self.assertEqual(speed_law.engine_clamp(0.25), 0.25)
        self.assertEqual(speed_law.engine_clamp(4.0), 4.0)

    def test_format_speed(self) -> None:
        self.assertEqual(speed_law.format_speed(1.0), "1.0×")
        self.assertEqual(speed_law.format_speed(1.25), "1.25×")
        self.assertEqual(speed_law.format_speed(0.75), "0.75×")
        self.assertEqual(speed_law.format_speed(2.0), "2.0×")

    def test_ui_speed_reexports(self) -> None:
        from tide.ui import speed as ui_speed
        self.assertIs(ui_speed.SPEED_PRESETS, speed_law.SPEED_PRESETS)
        self.assertEqual(ui_speed.SPEED_MIN, speed_law.SPEED_MIN)
        self.assertEqual(ui_speed.SPEED_MAX, speed_law.SPEED_MAX)
        self.assertEqual(ui_speed.SPEED_STEP, speed_law.SPEED_STEP)
        self.assertIs(ui_speed.format_speed, speed_law.format_speed)
        # The internal clamp is the same function object — one law.
        self.assertIs(ui_speed._clamp, speed_law.clamp)

    def test_router_clamps_to_engine_range(self) -> None:
        # The playback router's set_speed used to carry its own 0.25/4.0
        # literals; it now goes through speed_law. No backends registered
        # — the clamp + speed_changed emit happen regardless.
        _app()
        from tide.playback import PlaybackRouter
        router = PlaybackRouter()
        seen: list[float] = []
        router.speed_changed.connect(seen.append)
        router.set_speed(9.0)
        router.set_speed(0.05)
        router.set_speed(1.37)
        self.assertEqual(seen, [4.0, 0.25, 1.37])


if __name__ == "__main__":
    unittest.main()
