"""2.3 typography — roles, pairings, the stylesheet layer, the editors,
and the pre-2.3 bug where the font override never reached widgets.

Run offscreen:  QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest tests/test_typefaces.py
"""
import sys
import unittest
from unittest import mock

from PySide6.QtGui import QFont
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel

from tide import settings as settings_module, theming
from tide.settings import Settings
from tide.ui import typefaces


def _app() -> QApplication:
    return QApplication.instance() or QApplication(sys.argv[:1])


class RoleModelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.addCleanup(typefaces.set_roles, typefaces.roles())
        typefaces.set_roles({})

    def test_defaults_compact_to_nothing(self) -> None:
        self.assertEqual(typefaces.compact({}), {})
        self.assertTrue(typefaces.is_default(None))

    def test_normalize_clamps_and_drops(self) -> None:
        r = typefaces.normalize({"title": {"family": " Inter ", "weight": 650,
                                           "size": 999, "spacing": -99},
                                 "ui": {"size": 150}, "bogus": {}})
        self.assertEqual(r["title"]["family"], "Inter")
        self.assertEqual(r["title"]["weight"], 0)          # not a listed weight
        self.assertEqual(r["title"]["size"], typefaces.SIZE_RANGE[1])
        self.assertEqual(r["title"]["spacing"], typefaces.SPACING_RANGE[0])
        self.assertEqual(r["ui"]["size"], 100)             # body size is the pt row
        self.assertNotIn("bogus", r)

    def test_pairings_round_trip_and_match(self) -> None:
        for personality in ("modern", "brutalist"):
            for slug, _label, _spec in typefaces.pairings_for(personality):
                with self.subTest(p=personality, slug=slug):
                    roles = typefaces.pairing(personality, slug)
                    self.assertIsNotNone(roles)
                    fam = roles["ui"]["family"]
                    self.assertEqual(
                        typefaces.matching_pairing(personality, roles, fam), slug)
                    for role in ("title", "numbers"):
                        f = roles[role]["family"]
                        self.assertTrue(f == "" or f in typefaces.BUNDLED, f)

    def test_font_for(self) -> None:
        _app()
        typefaces.set_roles({"title": {"family": "Inter", "weight": 700,
                                       "size": 150, "spacing": 5}})
        base = QFont("IBM Plex Mono")
        base.setPointSizeF(10)
        f = typefaces.font_for("title", base)
        self.assertEqual(f.family(), "Inter")
        self.assertEqual(f.weight(), QFont.Weight.Bold)
        self.assertAlmostEqual(f.pointSizeF(), 15.0)
        self.assertAlmostEqual(f.letterSpacing(), 105.0)
        # roles at default change nothing
        typefaces.set_roles({})
        self.assertEqual(typefaces.font_for("title", base), base)

    def test_extra_qss(self) -> None:
        self.assertEqual(typefaces.extra_qss(10), "")
        typefaces.set_roles({"numbers": {"family": "JetBrains Mono", "size": 120}})
        qss = typefaces.extra_qss(10)
        self.assertIn('*[fontRole="numbers"]', qss)
        self.assertIn('"JetBrains Mono"', qss)
        self.assertIn("12.0pt", qss)


class ThemingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = _app()
        theming.register_bundled_fonts()
        theming.manager().refresh()
        self.addCleanup(theming.manager().apply_bundle, slug="brutalist-mono",
                        font_family="", font_size=0)
        self.addCleanup(typefaces.set_roles, {})

    def test_user_font_reaches_widgets(self) -> None:
        """Pre-2.3 the sheet's `* { font-family }` said the THEME's family,
        so the override only moved the app font underneath."""
        theming.manager().apply_bundle(slug="brutalist-mono", font_family="Inter",
                                       font_size=0)
        QTest.qWait(10)
        lbl = QLabel("x")
        lbl.ensurePolished()
        self.assertEqual(lbl.font().family(), "Inter")
        lbl.deleteLater()

    def test_role_rules_ride_the_sheet(self) -> None:
        typefaces.set_roles({"numbers": {"family": "JetBrains Mono"}})
        theming.manager().apply("brutalist-mono")
        QTest.qWait(10)
        self.assertIn('*[fontRole="numbers"]', self.app.styleSheet())
        lbl = QLabel("0:42")
        typefaces.tag(lbl, "numbers")
        lbl.ensurePolished()
        self.assertEqual(lbl.font().family(), "JetBrains Mono")
        lbl.deleteLater()


class EditorTests(unittest.TestCase):
    def setUp(self) -> None:
        _app()
        self._real = settings_module.save_fields
        settings_module.save_fields = lambda s, *n: None

    def tearDown(self) -> None:
        settings_module.save_fields = self._real
        QTest.qWait(10)

    def test_pairing_sets_roles_and_body_font(self) -> None:
        from tide.ui.settings import SettingsDialog
        s = Settings()
        s.preset = "modern"
        dlg = SettingsDialog(s)
        ed = dlg.widget_for("font_roles")
        idx = ed.pairing.findData("inter")
        ed.pairing.setCurrentIndex(idx)
        ed.pairing.activated.emit(idx)
        with mock.patch.object(dlg, "_apply_preview"):
            dlg._on_save()
        self.assertIn("font_roles", dlg.changed_keys())
        self.assertEqual(s.font_roles["numbers"]["family"], "JetBrains Mono")
        self.assertEqual(s.font_family_override, "Inter")

    def test_role_row_edit(self) -> None:
        from tide.ui.appearance_pickers import TypeRolesEditor
        ed = TypeRolesEditor("brutalist")
        hits = []
        ed.changed.connect(lambda: hits.append(1))
        ed.rows["title"]["size"].setValue(130)
        ed.rows["ui"]["spacing"].setValue(4)
        self.assertEqual(ed.value(), {"title": {"size": 130}, "ui": {"spacing": 4}})
        self.assertEqual(len(hits), 2)
        self.assertEqual(ed.pairing.currentData(), "")    # "custom"

    def test_font_browser_filters_and_picks(self) -> None:
        from tide.ui.appearance_pickers import FontBrowser
        b = FontBrowser("")
        fams = b.families()
        self.assertEqual(fams[0], "")                      # from theme
        self.assertEqual(fams[1:1 + len(typefaces.BUNDLED)], list(typefaces.BUNDLED))
        b.search.setText("plex")
        visible = [b.list.item(i).data(0x0100) for i in range(b.list.count())
                   if not b.list.item(i).isHidden()]
        self.assertTrue(visible and all("plex" in (v or "").lower() for v in visible))
        got = []
        b.picked.connect(got.append)
        b._accept_item(b.list.currentItem())
        self.assertTrue(got and "plex" in got[0].lower())

    def test_icon_set_picker(self) -> None:
        from tide.ui.appearance_pickers import IconSetPicker
        p = IconSetPicker()
        hits = []
        p.changed.connect(lambda: hits.append(1))
        p.rows["filled"].click()
        self.assertEqual(p.value(), "filled")
        p.rows["filled"].click()
        self.assertEqual(len(hits), 1)
        self.assertEqual(p.normalize("bogus"), "regular")


if __name__ == "__main__":
    unittest.main()
