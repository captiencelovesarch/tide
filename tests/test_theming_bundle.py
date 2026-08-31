"""apply_bundle + status tokens (tide 2.0 preset plumbing).

The preset switcher flips theme/font/size/case as one unit. Done through the
single setters that's four full re-applies — four queued repolishes, four
theme_changed emits, and widgets repainting against half-switched state in
between. apply_bundle must write the SAME state fields the setters own and
land the whole bundle in exactly one apply: one queued restyle, one emit.

Also covers the new status tokens: themes may declare [tokens] ok/warn/error,
status_color() and the @ok/@warn/@error QSS tokens resolve them with
dark/light-aware fallbacks when absent.

Run offscreen:  QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest tests/
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from tide import theming
from tide.theming import Theme


def _app() -> QApplication:
    return QApplication.instance() or QApplication(sys.argv[:1])


def _fake_theme(slug: str, *, tokens: dict | None = None, dark: bool = True,
                aesthetic: str = "modern") -> Theme:
    return Theme(
        slug=slug, name=slug, path=Path("/nonexistent") / slug,
        tokens=dict(tokens or {}), qss="", dark=dark, aesthetic=aesthetic,
    )


class BundleTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.app = _app()
        # Flush anything an earlier FILE left queued on the global manager
        # (a late palette worker can queue right as that file's last drain
        # closes) before arming the spy — same guard as restyle_coalesce.
        QTest.qWait(20)
        # Fresh manager per test so scheduled flushes can't leak across tests.
        self.mgr = theming.ThemeManager()
        self.mgr.refresh()
        self.spy = mock.patch.object(self.app, "setStyleSheet").start()
        self.addCleanup(mock.patch.stopall)
        # Case override is module state shared with the global manager — pin
        # and restore it so tests can't leak casing into each other.
        self._saved_case = theming._CASE_OVERRIDE
        theming._CASE_OVERRIDE = ""
        self.emits: list[object] = []
        self.mgr.theme_changed.connect(self.emits.append)

    def tearDown(self) -> None:
        theming._CASE_OVERRIDE = self._saved_case
        # Drain any pending flush so it fires against this test's spy.
        QTest.qWait(20)

    def _pump(self) -> None:
        QTest.qWait(20)


class ApplyBundleTest(BundleTestBase):
    def test_full_bundle_costs_one_apply(self) -> None:
        self.mgr.apply("brutalist-mono")
        self._pump()
        self.spy.reset_mock()
        self.emits.clear()

        out = self.mgr.apply_bundle(
            slug="nord", font_family="Bundle Test Face",
            font_size=13, case="upper",
        )
        self.assertIsNotNone(out)
        self.assertEqual(out.slug, "nord")
        self.assertEqual(len(self.emits), 1,
                         "a full bundle must emit theme_changed exactly once")
        self.assertEqual(self.spy.call_count, 0,
                         "apply_bundle pushed QSS inside the calling turn")
        self.assertTrue(self.mgr._restyle_scheduled,
                        "the one restyle must be queued, not skipped")
        self._pump()
        self.assertEqual(self.spy.call_count, 1,
                         "a full bundle must cost exactly one repolish")
        # All axes actually landed on the setter-owned state.
        self.assertEqual(self.mgr.user_font(), "Bundle Test Face")
        self.assertEqual(self.mgr.user_font_size(), 13)
        self.assertEqual(theming._CASE_OVERRIDE, "upper")

    def test_bundle_font_reaches_the_queued_font(self) -> None:
        self.mgr.apply_bundle(slug="brutalist-mono",
                              font_family="Bundle Test Face", font_size=13)
        font = self.mgr._pending_font
        self.assertIsNotNone(font, "bundle with a font must queue app.setFont")
        self.assertEqual(font.family(), "Bundle Test Face")
        self._pump()

    def test_none_leaves_an_axis_untouched(self) -> None:
        self.mgr.apply_bundle(slug="brutalist-mono",
                              font_family="Keep Me", font_size=14, case="upper")
        self._pump()
        self.mgr.apply_bundle(slug="nord")   # all axes default to None
        self.assertEqual(self.mgr.user_font(), "Keep Me")
        self.assertEqual(self.mgr.user_font_size(), 14)
        self.assertEqual(theming._CASE_OVERRIDE, "upper")
        self._pump()

    def test_falsy_values_clear(self) -> None:
        self.mgr.apply_bundle(slug="brutalist-mono",
                              font_family="Clear Me", font_size=14, case="upper")
        self._pump()
        self.mgr.apply_bundle(font_family="", font_size=0, case="")
        self.assertEqual(self.mgr.user_font(), "")
        self.assertEqual(self.mgr.user_font_size(), 0)
        self.assertEqual(theming._CASE_OVERRIDE, "")
        self._pump()

    def test_case_routes_through_styled_case(self) -> None:
        self.mgr.apply_bundle(slug="brutalist-mono", case="upper")
        self.assertEqual(theming.styled_case("hello"), "HELLO")
        self.mgr.apply_bundle(case="")
        self.assertEqual(theming.styled_case("HeLLo",
                                             self.mgr.current()), "hello")
        self._pump()

    def test_invalid_case_clears_like_set_case_override(self) -> None:
        theming._CASE_OVERRIDE = "upper"
        self.mgr.apply_bundle(slug="brutalist-mono", case="wingdings")
        self.assertEqual(theming._CASE_OVERRIDE, "")
        self._pump()

    def test_no_slug_reapplies_current(self) -> None:
        self.mgr.apply("brutalist-mono")
        self._pump()
        self.emits.clear()
        out = self.mgr.apply_bundle(font_size=15)
        self.assertEqual(out.slug, "brutalist-mono")
        self.assertEqual(len(self.emits), 1)
        self._pump()

    def test_no_slug_no_current_stores_state_without_applying(self) -> None:
        out = self.mgr.apply_bundle(font_family="Early Bird", case="upper")
        self.assertIsNone(out)
        self.assertEqual(self.emits, [], "nothing to apply → nothing to emit")
        self.assertFalse(self.mgr._restyle_scheduled)
        # ...but the state waits for the first real apply.
        self.assertEqual(self.mgr.user_font(), "Early Bird")
        self.mgr.apply("brutalist-mono")
        self.assertEqual(self.mgr._pending_font.family(), "Early Bird")
        self._pump()

    def test_unknown_slug_returns_none(self) -> None:
        self.mgr.apply("brutalist-mono")
        self._pump()
        self.emits.clear()
        self.assertIsNone(self.mgr.apply_bundle(slug="no-such-theme"))
        self.assertEqual(self.emits, [])

    def test_dynamic_overrides_survive_same_slug_bundle(self) -> None:
        self.mgr.apply("brutalist-mono")
        self._pump()
        self.mgr.override_tokens({"accent": "#abcdef"})
        out = self.mgr.apply_bundle(slug="brutalist-mono", font_size=13)
        self.assertEqual(out.token("accent"), "#abcdef",
                         "re-applying the same slug must not drop adaptive")
        self._pump()

    def test_dynamic_overrides_cleared_on_slug_change(self) -> None:
        self.mgr.apply("brutalist-mono")
        self._pump()
        self.mgr.override_tokens({"accent": "#abcdef"})
        out = self.mgr.apply_bundle(slug="nord")
        self.assertNotEqual(out.token("accent"), "#abcdef",
                            "slug change must clear dynamic overrides so "
                            "adaptive re-anchors against the new palette")
        self.assertEqual(self.mgr._dynamic_overrides, {})
        self._pump()

    def test_user_overrides_merge_remove_and_survive(self) -> None:
        out = self.mgr.apply_bundle(slug="brutalist-mono",
                                    user_overrides={"radius": "9px"})
        self.assertEqual(out.token("radius"), "9px")
        # sticky across a theme switch, just like set_user_override
        out = self.mgr.apply_bundle(slug="nord")
        self.assertEqual(out.token("radius"), "9px")
        # None value removes the key
        out = self.mgr.apply_bundle(user_overrides={"radius": None})
        self.assertNotEqual(out.token("radius"), "9px")
        self._pump()

    def test_single_setters_still_work_after_bundle(self) -> None:
        self.mgr.apply_bundle(slug="brutalist-mono", font_family="First")
        self._pump()
        self.emits.clear()
        self.mgr.set_user_font("Second")
        self.assertEqual(self.mgr.user_font(), "Second")
        self.assertEqual(len(self.emits), 1,
                         "set_user_font must keep re-applying on its own")
        self.mgr.set_user_font_size(18)
        self.assertEqual(self.mgr.user_font_size(), 18)
        self.assertEqual(len(self.emits), 2)
        self._pump()


class StatusTokenTest(BundleTestBase):
    def setUp(self) -> None:
        super().setUp()
        # status_color consults the process-global manager — point it at this
        # test's fresh instance and restore after.
        self._saved_mgr = theming._manager
        theming._manager = self.mgr
        self.addCleanup(self._restore_manager)

    def _restore_manager(self) -> None:
        theming._manager = self._saved_mgr

    def test_declared_tokens_win(self) -> None:
        theme = _fake_theme("statusful", tokens={
            "ok": "#00ff00", "warn": "#ffff00", "error": "#ff0000",
        })
        self.mgr._themes["statusful"] = theme
        self.mgr.apply("statusful")
        self.assertEqual(theming.status_color("ok"), "#00ff00")
        self.assertEqual(theming.status_color("warn"), "#ffff00")
        self.assertEqual(theming.status_color("error"), "#ff0000")
        self._pump()

    def test_dark_fallbacks_match_the_source_panel_family(self) -> None:
        self.mgr.apply("brutalist-mono")   # dark, declares no status tokens
        self.assertEqual(theming.status_color("ok"), "#5aaf6a")
        self.assertEqual(theming.status_color("warn"), "#d4b95e")
        self.assertEqual(theming.status_color("error"), "#a05a5a")
        self._pump()

    def test_light_themes_get_the_light_fallbacks(self) -> None:
        self.mgr.apply("paper")   # dark = false
        for kind in ("ok", "warn", "error"):
            self.assertEqual(theming.status_color(kind),
                             theming._STATUS_FALLBACKS_LIGHT[kind])
            self.assertNotEqual(theming.status_color(kind),
                                theming._STATUS_FALLBACKS_DARK[kind])
        self._pump()

    def test_no_current_theme_falls_back_dark(self) -> None:
        self.assertEqual(theming.status_color("ok"),
                         theming._STATUS_FALLBACKS_DARK["ok"])

    def test_unknown_kind_returns_a_parseable_color(self) -> None:
        from PySide6.QtGui import QColor
        self.mgr.apply("brutalist-mono")
        self.assertTrue(QColor(theming.status_color("mystery")).isValid())
        self._pump()

    def test_overrides_can_retint_status(self) -> None:
        self.mgr.apply("brutalist-mono")
        self.mgr.set_user_override("warn", "#123456")
        self.assertEqual(theming.status_color("warn"), "#123456")
        self._pump()

    def test_substitute_exposes_status_tokens(self) -> None:
        qss = "QLabel { color: @ok; } /* @warn @error */"
        dark = _fake_theme("d", dark=True)
        out = theming._substitute(qss, dark)
        self.assertIn("#5aaf6a", out)
        self.assertIn("#d4b95e", out)
        self.assertIn("#a05a5a", out)
        self.assertNotIn("@ok", out)
        light = _fake_theme("l", dark=False)
        out = theming._substitute(qss, light)
        self.assertIn(theming._STATUS_FALLBACKS_LIGHT["ok"], out)
        self.assertNotIn("@ok", out)

    def test_substitute_prefers_declared_status_tokens(self) -> None:
        theme = _fake_theme("s", tokens={"ok": "#010203"})
        out = theming._substitute("QLabel { color: @ok; }", theme)
        self.assertIn("#010203", out)
        self.assertNotIn("#5aaf6a", out)


if __name__ == "__main__":
    unittest.main()
