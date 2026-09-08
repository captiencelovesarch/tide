"""Track-change text transitions: scramble, sweep, rise, off, and the
motion switch over all of them.

Run offscreen:  QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest tests/test_text_fx.py
"""
import sys
import unittest
from unittest import mock

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFontMetrics, QImage, QPainter
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

from tide import theming
from tide.ui import motion, text_fx
from tide.ui.variants import make_now_label


def _app() -> QApplication:
    return QApplication.instance() or QApplication(sys.argv[:1])


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        _app()
        theming.manager().refresh()
        theming.manager().apply("nord")

    def setUp(self) -> None:
        self._intensity = motion.intensity()
        self._profile = motion.bound_profile()
        self._style = text_fx.style()
        motion._reduced_motion = False
        motion.set_intensity("full")
        motion.set_profile("springy")
        self._widgets = []

    def tearDown(self) -> None:
        text_fx.set_style(self._style)
        motion.set_intensity(self._intensity)
        motion.set_profile(None)
        motion.bind_preset(self._profile)
        for w in self._widgets:
            w.deleteLater()
        QTest.qWait(5)


class RegistryTest(_Base):
    def test_styles_and_choices(self) -> None:
        self.assertEqual(text_fx.STYLES, ("scramble", "sweep", "rise", "off"))
        self.assertEqual([k for k, _ in text_fx.choices()], list(text_fx.STYLES))
        text_fx.set_style("rise")
        self.assertEqual(text_fx.style(), "rise")
        text_fx.set_style("nonsense")
        self.assertEqual(text_fx.style(), "scramble")

    def test_motion_off_makes_every_style_instant(self) -> None:
        for st in text_fx.STYLES:
            text_fx.set_style(st)
            motion.set_intensity("full")
            self.assertEqual(text_fx.effective_style(), st)
            motion.set_intensity("off")
            self.assertEqual(text_fx.effective_style(), "off")

    def test_setting_is_registered_with_a_live_applier(self) -> None:
        from tide.settings import Settings
        from tide.ui import settings_schema
        desc = next(d for d in settings_schema.REGISTRY if d.key == "text_transition")
        self.assertEqual(desc.live, "apply_text_transition_setting")
        self.assertEqual(settings_schema.text_transition_choices(), text_fx.choices())
        self.assertEqual(Settings().text_transition, "scramble")


def _render(fn, w=320, h=40):
    img = QImage(w, h, QImage.Format_ARGB32)
    img.fill(QColor(0, 0, 0))
    p = QPainter(img)
    fn(p, QRectF(0, 0, w, h))
    p.end()
    return img


class PaintersTest(_Base):
    def test_sweep_and_rise_move_with_progress(self) -> None:
        w = QWidget()
        self._widgets.append(w)
        fm = QFontMetrics(w.font())
        fg, accent = QColor("#e6e6e6"), QColor("#9fdcec")
        for painter in ("sweep", "rise"):
            with self.subTest(style=painter):
                frames = []
                for prog in (0.0, 0.35, 0.7, 1.0):
                    def draw(p, r, prog=prog):
                        p.setFont(w.font())
                        if painter == "sweep":
                            text_fx.paint_sweep(p, r, "old song title", "new song title", prog, fm, fg, accent, Qt.AlignLeft)
                        else:
                            text_fx.paint_rise(p, r, "old song title", "new song title", prog, fm, fg, Qt.AlignLeft)
                    frames.append(_render(draw))
                self.assertNotEqual(frames[0], frames[1])
                self.assertNotEqual(frames[1], frames[2])
                self.assertNotEqual(frames[2], frames[3])
                # the end state is just the new text in fg: no accent pixels left
                end = frames[3]
                hits = sum(1 for y in range(0, end.height(), 2) for x in range(0, end.width(), 2)
                           if end.pixelColor(x, y).blue() > end.pixelColor(x, y).red() + 40)
                self.assertEqual(hits, 0, "glow survived to the end")

    def test_reveal_runs_then_hands_back(self) -> None:
        w = QWidget()
        self._widgets.append(w)
        text_fx.set_style("sweep")
        r = text_fx.TextReveal(w, "t")
        self.assertFalse(r.active())
        r.start("before")
        self.assertTrue(r.active())
        QTest.qWait(motion.dur("med") + 150)
        self.assertFalse(r.active())
        self.assertEqual(r.progress(), 1.0)
        text_fx.set_style("off")
        r.start("x")
        self.assertFalse(r.active())
        text_fx.set_style("scramble")
        r.start("x")
        self.assertFalse(r.active(), "scramble is driven by text frames, not a reveal")


class NowLabelTest(_Base):
    def _label(self, slug):
        lbl = make_now_label(slug)
        self._widgets.append(lbl)
        lbl.resize(420, 60)
        lbl.show()
        QTest.qWait(5)
        lbl.setTrack("radiohead", "creep", "pablo honey")
        lbl.repaint()
        return lbl

    def test_every_variant_reveals_on_sweep_and_rise(self) -> None:
        for style in ("sweep", "rise"):
            text_fx.set_style(style)
            for slug in ("stacked", "inline", "centered", "headline"):
                with self.subTest(style=style, variant=slug):
                    lbl = self._label(slug)
                    self.assertTrue(lbl._painted["primary"])
                    lbl.setTrackAnimated("deftones", "sextape", "saturday night wrist")
                    self.assertTrue(lbl._reveal["primary"].active())
                    self.assertTrue(lbl._reveal["secondary"].active())   # FULL runs both
                    self.assertEqual(lbl._title, "sextape")             # value lands at once
                    lbl.repaint()
                    mid = lbl.grab().toImage()
                    QTest.qWait(motion.dur("med") + 300)
                    self.assertFalse(lbl._reveal["primary"].active())
                    lbl.repaint()
                    self.assertNotEqual(mid, lbl.grab().toImage())
                    self.assertIn("sextape", lbl._painted["primary"])

    def test_lite_reveals_the_primary_line_only(self) -> None:
        text_fx.set_style("rise")
        motion.set_intensity("lite")
        lbl = self._label("stacked")
        lbl.setTrackAnimated("deftones", "sextape", "saturday night wrist")
        self.assertTrue(lbl._reveal["primary"].active())
        self.assertFalse(lbl._reveal["secondary"].active())

    def test_scramble_path_is_unchanged(self) -> None:
        text_fx.set_style("scramble")
        lbl = self._label("stacked")
        with mock.patch.object(motion, "scramble_text") as scr:
            lbl.setTrackAnimated("deftones", "sextape", "")
            self.assertGreaterEqual(scr.call_count, 1)
        self.assertFalse(lbl._reveal["primary"].active())

    def test_off_just_sets(self) -> None:
        text_fx.set_style("off")
        lbl = self._label("stacked")
        with mock.patch.object(motion, "scramble_text") as scr:
            lbl.setTrackAnimated("deftones", "sextape", "")
            scr.assert_not_called()
        self.assertEqual(lbl._title, "sextape")
        self.assertFalse(lbl._reveal["primary"].active())


class RevealLabelTest(_Base):
    def test_label_reveals_scrambles_or_snaps_by_style(self) -> None:
        host = QWidget()
        self._widgets.append(host)
        lbl = text_fx.RevealLabel("first", host)
        host.show()
        QTest.qWait(5)
        text_fx.set_style("rise")
        text_fx.animate_label(lbl, "second", owner=host, kind="k")
        self.assertEqual(lbl.text(), "second")
        self.assertTrue(lbl._reveal.active())
        lbl.repaint()
        QTest.qWait(motion.dur("med") + 200)
        self.assertFalse(lbl._reveal.active())
        text_fx.set_style("scramble")
        with mock.patch.object(motion, "scramble_text") as scr:
            text_fx.animate_label(lbl, "third", owner=host, kind="k")
            scr.assert_called_once()
        text_fx.set_style("off")
        text_fx.animate_label(lbl, "fourth", owner=host, kind="k")
        self.assertEqual(lbl.text(), "fourth")
        self.assertFalse(lbl._reveal.active())

    def test_mini_and_fullscreen_labels_are_reveal_labels(self) -> None:
        import inspect
        from tide.ui import mini, fullscreen
        for mod in (mini, fullscreen):
            src = inspect.getsource(mod)
            self.assertIn("text_fx.RevealLabel(", src, mod.__name__)
            self.assertNotIn("motion_module.scramble_text(label.setText", src, mod.__name__)


if __name__ == "__main__":
    unittest.main()
