"""Headings and the hero render per aesthetic; the strip files "next"
under the title in modern.

Run offscreen:  QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest tests/test_headings_hero.py
"""
import sys
import unittest
from unittest import mock

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel

from tide import glyphs, theming
from tide.ui import scale
from tide.ui.headings import Heading, line_heading
from tide.ui.home import patterns


def _app() -> QApplication:
    return QApplication.instance() or QApplication(sys.argv[:1])


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        _app()
        theming.manager().refresh()

    def setUp(self) -> None:
        self._sheet = mock.patch.object(QApplication.instance(), "setStyleSheet")
        self._sheet.start()
        self._widgets = []

    def tearDown(self) -> None:
        self._sheet.stop()
        for w in self._widgets:
            w.deleteLater()
        QTest.qWait(5)

    def _apply(self, slug: str) -> None:
        theming.manager().apply(slug)
        QTest.qWait(5)

    def _keep(self, w):
        self._widgets.append(w)
        return w


class HeadingTest(_Base):
    def test_brutalist_keeps_the_rule_and_modern_drops_it(self) -> None:
        self._apply("brutalist-mono")
        h = self._keep(Heading("your library · 12"))
        self.assertEqual(h.text(), line_heading("your library · 12"))
        self.assertEqual(h.property("class"), "dim")
        self._apply("nord")
        self.assertEqual(h.text(), theming.styled_case("your library · 12"))
        self.assertNotIn(glyphs.glyph("heading_dash"), h.text())
        self.assertEqual(h.property("class"), "title")
        self.assertGreater(h.contentsMargins().top(), 0)
        self._apply("brutalist-mono")
        self.assertEqual(h.text(), line_heading("your library · 12"))
        self.assertEqual(h.contentsMargins().top(), 0)

    def test_set_label_rerenders_in_the_current_face(self) -> None:
        self._apply("nord")
        h = self._keep(Heading("home"))
        h.set_label("home · loading…")
        self.assertEqual(h.text(), theming.styled_case("home · loading…"))
        self.assertEqual(h.label(), "home · loading…")
        self._apply("brutalist-mono")
        h.set_label("home · 3")
        self.assertEqual(h.text(), line_heading("home · 3"))

    def test_total_width_is_honoured_in_brutalist(self) -> None:
        self._apply("brutalist-mono")
        h = self._keep(Heading("colors", 34))
        self.assertEqual(h.text(), line_heading("colors", 34))

    def test_views_use_the_widget(self) -> None:
        import inspect
        from tide.ui import album, artist, history, library, lyrics, song_page, settings, window
        from tide.ui.home import view as home_view
        for mod in (album, artist, history, library, lyrics, song_page, settings, window, home_view):
            with self.subTest(module=mod.__name__):
                src = inspect.getsource(mod)
                self.assertNotIn("QLabel(_line_heading(", src)
                self.assertNotIn("QLabel(line_heading(", src)
                self.assertNotIn(".setText(_line_heading(", src)
                self.assertNotIn(".setText(self._line_heading(", src)


class _Track:
    def __init__(self):
        self.thumbnail = "https://example.invalid/a.jpg"
        self.artists = "radiohead"
        self.title = "creep"


class HeroTest(_Base):
    def _hero(self):
        with mock.patch("tide.ui.art_cache.cache") as cache:
            cache.return_value.request.return_value = None
            cache.return_value.image_loaded = mock.MagicMock()
            return self._keep(patterns.Hero("good evening, cap", "about 3h this week",
                                            _Track(), can_resume=True, show_likes=True))

    def test_modern_hero_is_the_big_moment(self) -> None:
        self._apply("nord")
        h = self._hero()
        self.assertEqual(h._art_px, scale.px(patterns.Hero.ART_MODERN))
        self.assertTrue(h._modern)
        self.assertGreater(h.layout().contentsMargins().left(), 0)
        title = next(l for l in h.findChildren(QLabel)
                     if l.text() == theming.styled_case("good evening, cap"))
        self.assertEqual(title.property("class"), "display")
        h.resize(600, 220)
        h.show()
        QTest.qWait(10)
        self.assertFalse(h.grab().toImage().isNull())

    def test_hero_follows_a_theme_flip_and_the_radius(self) -> None:
        self._apply("nord")
        h = self._hero()
        self._apply("brutalist-mono")
        self.assertFalse(h._modern)
        self.assertEqual(h._art_px, scale.px(patterns.Hero.ART))
        title = next(l for l in h.findChildren(QLabel)
                     if l.text() == theming.styled_case("good evening, cap"))
        self.assertTrue(title.font().bold())
        self._apply("nord")
        self.assertTrue(h._modern)
        self.assertEqual(h._art_px, scale.px(patterns.Hero.ART_MODERN))
        self.assertEqual(title.property("class"), "display")
        # the user's corner style reaches the hero through the effective theme
        theming.manager().set_user_override("radius", "12px")
        try:
            QTest.qWait(5)
            self.assertEqual(theming.effective_radius_px(h._theme), 12)
        finally:
            theming.manager().set_user_override("radius", None)

    def test_brutalist_hero_is_unchanged(self) -> None:
        self._apply("brutalist-mono")
        h = self._hero()
        self.assertEqual(h._art_px, scale.px(patterns.Hero.ART))
        self.assertFalse(h._modern)
        self.assertEqual(h.layout().contentsMargins().left(), 0)
        title = next(l for l in h.findChildren(QLabel)
                     if l.text() == theming.styled_case("good evening, cap"))
        self.assertIsNone(title.property("class"))
        self.assertTrue(title.font().bold())


class StripOrderTest(unittest.TestCase):
    def setUp(self) -> None:
        _app()
        self._sheet = mock.patch.object(QApplication.instance(), "setStyleSheet")
        self._sheet.start()
        theming.manager().refresh()

    def tearDown(self) -> None:
        self._sheet.stop()

    def _window(self, slug):
        theming.manager().apply(slug)
        from tide.playback import MpvBackend, PlaybackRouter
        from tide.sources.local import LocalSource
        from tide.ui.window import MainWindow
        router = PlaybackRouter()
        router.register(MpvBackend())
        return MainWindow(LocalSource(), router)

    def test_modern_files_next_under_the_title_and_flips_live(self) -> None:
        w = self._window("nord")
        try:
            col = w._strip_right_col
            self.assertEqual(col.indexOf(w.now_label), 0)
            self.assertEqual(col.indexOf(w.up_next), 1)
            theming.manager().apply("brutalist-mono")
            QTest.qWait(10)
            self.assertEqual(col.indexOf(w.up_next), 0)
            self.assertEqual(col.indexOf(w.now_label), 1)
            theming.manager().apply("nord")
            QTest.qWait(10)
            self.assertEqual(col.indexOf(w.up_next), 1)
        finally:
            w.close()
            QTest.qWait(20)

    def test_brutalist_builds_next_first(self) -> None:
        w = self._window("brutalist-mono")
        try:
            self.assertEqual(w._strip_right_col.indexOf(w.up_next), 0)
        finally:
            w.close()
            QTest.qWait(20)


if __name__ == "__main__":
    unittest.main()
