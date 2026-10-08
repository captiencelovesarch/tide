"""Text reads the patch of backdrop it sits on.

The reported bug: with a red cover on the adaptive backdrop, dim text,
chart ranks and the strip's meta line vanished into the red, while the
same greys read fine on the dark side of the same window. One ``dim``
can't be right for both, so each run of text now asks the backdrop what
is behind it (``CentralBg.ink_range``) and moves its own ink just far
enough (``contrast.legible_ink``). Text on a patch the theme already
works on keeps the theme's exact colour.

Run offscreen:  QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest tests/test_local_ink.py
"""
import sys
import time
import unittest

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QProxyStyle,
    QStyleFactory,
    QVBoxLayout,
    QWidget,
)

from tide import contrast, theming


def _app() -> QApplication:
    return QApplication.instance() or QApplication(sys.argv[:1])


RED = QColor(233, 26, 12)          # the cover in the bug report
DARK = QColor(30, 6, 6)
THEME_BG = QColor("#0a0a0a")
DIM = QColor("#5f5f5f")
FG = QColor("#f0f0f0")


def _y(c: QColor) -> float:
    return contrast.apca_y(c)


class ApcaTests(unittest.TestCase):
    """The APCA maths against the reference values its authors publish."""

    def test_reference_pairs(self) -> None:
        for text, bg, lc in (("#888888", "#ffffff", 63.1),
                             ("#ffffff", "#888888", -68.5),
                             ("#000000", "#aaaaaa", 58.1),
                             ("#aaaaaa", "#000000", -56.2),
                             ("#112233", "#ddeeff", 91.7),
                             ("#ddeeff", "#112233", -93.1)):
            got = contrast.apca_lc(_y(QColor(text)), _y(QColor(bg)))
            self.assertAlmostEqual(got, lc, places=1, msg=f"{text} on {bg}")

    def test_white_on_red_beats_black_on_red(self) -> None:
        # The reason this is APCA and not the WCAG ratio, which ranks
        # these the other way round and would flip white titles to black.
        white = abs(contrast.apca_lc(1.0, _y(RED)))
        black = abs(contrast.apca_lc(0.0, _y(RED)))
        self.assertGreater(white, 60)
        self.assertLess(black, 40)
        self.assertGreater(contrast.contrast_ratio(QColor("#000"), RED),
                           contrast.contrast_ratio(QColor("#fff"), RED))

    def test_too_close_reads_as_zero(self) -> None:
        self.assertEqual(contrast.apca_lc(0.18, 0.17), 0.0)


class LegibleInkTests(unittest.TestCase):
    def _on(self, ink: QColor, patch: QColor, prefer: int = 0, ref=THEME_BG):
        y = _y(patch)
        return contrast.legible_ink(ink, y, y, _y(ref), prefer)

    def test_ink_on_its_own_theme_bg_is_untouched(self) -> None:
        for ink in (DIM, FG, QColor("#9b87f5")):
            out, side = self._on(ink, THEME_BG)
            self.assertEqual(side, 0)
            self.assertEqual(out.name(), ink.name())

    def test_dim_on_red_goes_light_and_reads(self) -> None:
        out, side = self._on(DIM, RED)
        self.assertEqual(side, 1, "dim went darker: dark grey on red is mud")
        y = _y(RED)
        self.assertGreaterEqual(contrast.worst_lc(_y(out), y, y),
                                contrast.LOCAL_FLOOR_MIN - 0.5)

    def test_white_title_stays_white_on_red(self) -> None:
        out, side = self._on(FG, RED)
        self.assertEqual(side, 0)
        self.assertEqual(out.name(), FG.name())

    def test_fg_on_yellow_goes_dark(self) -> None:
        out, side = self._on(FG, QColor(255, 220, 0))
        self.assertEqual(side, -1)
        self.assertLess(_y(out), 0.2)

    def test_a_small_shortfall_moves_a_little(self) -> None:
        # Dark red is only a little brighter than the theme bg: dim gets
        # a nudge, not the full lift a red patch needs.
        nudged, side = self._on(DIM, QColor(124, 41, 7))
        lifted, _ = self._on(DIM, RED)
        self.assertEqual(side, 1)
        self.assertGreater(nudged.lightness(), DIM.lightness())
        self.assertLess(nudged.lightness(), lifted.lightness())

    def test_light_theme_ink_on_a_dark_patch_goes_light(self) -> None:
        out, side = self._on(QColor("#222222"), QColor(20, 20, 40),
                             ref=QColor("#f5f2ea"))
        self.assertEqual(side, 1)
        self.assertGreater(_y(out), 0.3)

    def test_mid_tone_keeps_its_side(self) -> None:
        # White and black both land near 50 on orange: a white title
        # stays white instead of flipping for a few points.
        out, side = self._on(FG, QColor(255, 140, 0))
        self.assertEqual(side, 1)
        self.assertEqual(out.name(), "#ffffff")

    def test_last_side_sticks_when_close(self) -> None:
        patch = QColor(255, 150, 60)
        free, free_side = self._on(DIM, patch)
        kept, kept_side = self._on(DIM, patch, prefer=-free_side)
        # Either the other side is still within the stick margin and the
        # ink stays put, or it isn't and the ink is free to move.
        self.assertIn(kept_side, (free_side, -free_side))
        if kept_side == -free_side:
            self.assertNotEqual(kept.name(), free.name())


def _split_frame(self, w: int, h: int) -> QImage:
    """Backdrop stand-in: dark left half, the report's red right half."""
    img = QImage(w, h, QImage.Format_RGB32)
    p = QPainter(img)
    p.fillRect(0, 0, w // 2, h, DARK)
    p.fillRect(w // 2, 0, w - w // 2, h, RED)
    p.end()
    return img


class _Scene:
    """A CentralBg (frame patched to the split) around two columns of
    labels, painted once so the probe has a frame to read."""

    def __init__(self, extra=None) -> None:
        from tide.ui.central_bg import CentralBg
        _app()
        self._orig = CentralBg._render_buffer
        CentralBg._render_buffer = _split_frame
        self.shell = QWidget()
        lay = QHBoxLayout(self.shell)
        self.left, self.right = QLabel("left text"), QLabel("right text")
        for lab in (self.left, self.right):
            col = QVBoxLayout()
            col.addWidget(lab)
            lay.addLayout(col)
        if extra is not None:
            extra(self, lay)
        self.bg = CentralBg(self.shell)
        self.bg.set_enabled(True)
        self.bg.resize(400, 80)
        self.bg.show()
        _app().processEvents()
        self.bg.grab()

    def close(self) -> None:
        from tide.ui.central_bg import CentralBg
        CentralBg._render_buffer = self._orig
        self.bg.hide()
        self.bg.deleteLater()
        _app().processEvents()


class ProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scene = _Scene()

    def tearDown(self) -> None:
        self.scene.close()

    def test_each_half_reads_its_own_patch(self) -> None:
        bg = self.scene.bg
        lo_l, hi_l = bg.ink_range(QRect(10, 10, 100, 20))
        lo_r, hi_r = bg.ink_range(QRect(290, 10, 100, 20))
        self.assertLess(hi_l, 0.01)
        self.assertAlmostEqual(lo_r, _y(RED), places=2)

    def test_a_rect_across_the_seam_spans_both(self) -> None:
        lo, hi = self.scene.bg.ink_range(QRect(150, 10, 100, 20))
        self.assertLess(lo, 0.01)
        self.assertGreater(hi, 0.15)

    def test_no_range_with_the_backdrop_off(self) -> None:
        bg = self.scene.bg
        bg.set_enabled(False)
        bg.grab()
        self.assertIsNone(bg.ink_range(QRect(290, 10, 100, 20)))

    def test_a_flash_eases_in_instead_of_landing_at_once(self) -> None:
        bg = self.scene.bg
        before = bg.ink_range(QRect(10, 10, 50, 20))[1]
        white = QImage(bg._ink_src.size(), QImage.Format_RGB32)
        white.fill(QColor("#ffffff"))
        bg._ink_src = white
        bg._ink_frame += 1
        # 150 ms into a bass hit (the grid rebuilds at most every 100 ms)
        bg._ink_t = time.monotonic() - 0.15
        after = bg.ink_range(QRect(10, 10, 50, 20))[1]
        self.assertGreater(after, before)
        self.assertLess(after, 0.5)


class LabelInkTests(unittest.TestCase):
    def setUp(self) -> None:
        from tide.ui import legibility
        _app()
        self.legibility = legibility
        self._was = theming.manager().text_contrast()
        theming.manager().set_text_contrast(True)
        legibility.reset_caches()
        self.scene = _Scene()

    def tearDown(self) -> None:
        self.scene.close()
        theming.manager().set_text_contrast(self._was)
        self.legibility.reset_caches()

    def test_only_the_label_on_red_moves(self) -> None:
        ink = self.legibility.ink
        self.assertEqual(ink(self.scene.left, DIM).name(), DIM.name())
        moved = ink(self.scene.right, DIM)
        self.assertGreater(moved.lightness(), 150)

    def test_setting_off_leaves_every_ink_alone(self) -> None:
        theming.manager().set_text_contrast(False)
        self.assertEqual(self.legibility.ink(self.scene.right, DIM).name(),
                         DIM.name())

    def test_an_opaque_panel_stops_the_lookup(self) -> None:
        self.scene.right.setProperty(self.legibility.OPAQUE_PROPERTY, True)
        self.legibility.reset_caches()
        self.assertEqual(self.legibility.ink(self.scene.right, DIM).name(),
                         DIM.name())

    def test_an_ink_meant_for_a_fill_is_left_alone(self) -> None:
        facts = self.legibility._theme_facts()
        facts.on_fill = {DIM.rgb()}
        try:
            self.assertEqual(self.legibility.ink(self.scene.right, DIM).name(),
                             DIM.name())
        finally:
            facts.refresh()

    def test_a_widget_in_another_window_is_left_alone(self) -> None:
        loose = QLabel("dialog text")
        self.assertEqual(self.legibility.ink(loose, DIM).name(), DIM.name())

    def test_the_style_proxy_paints_the_moved_ink(self) -> None:
        proxy = self.legibility.InkStyle(QStyleFactory.create("Fusion"))
        lab = self.scene.right
        lab.setStyle(proxy)
        lab.setStyleSheet(f"color: {DIM.name()}; background: transparent;")
        _app().processEvents()
        img = self.scene.bg.grab().toImage()
        origin = lab.mapTo(self.scene.bg, lab.rect().topLeft())
        brightest = 0
        for y in range(origin.y(), origin.y() + lab.height()):
            for x in range(origin.x(), origin.x() + lab.width()):
                c = QColor(img.pixel(x, y))
                brightest = max(brightest, min(c.green(), c.blue()))
        # Red's green/blue sit near 20; the theme's dim tops out at 95.
        self.assertGreater(brightest, 150)

    def test_proxy_passes_untouched_text_through(self) -> None:
        calls = []

        class Spy(QProxyStyle):
            def drawItemText(self, painter, rect, flags, pal, enabled, text,
                             textRole=None):
                calls.append(pal.color(textRole).name())

        proxy = self.legibility.InkStyle(Spy(QStyleFactory.create("Fusion")))
        lab = self.scene.left
        lab.setStyle(proxy)
        lab.setStyleSheet(f"color: {DIM.name()}; background: transparent;")
        _app().processEvents()
        self.scene.bg.grab()
        self.assertIn(DIM.name(), calls)


class FadeTests(unittest.TestCase):
    """An ink that has to move eases there over the motion profile's "med"
    duration instead of jumping (the first build swapped instantly, and
    a run crossing from one side of a patch to the other looked like a
    glitch). Text that just appeared lands on its ink with no fade, and
    motion off keeps every change instant."""

    def setUp(self) -> None:
        from tide.ui import legibility, motion
        _app()
        self.legibility, self.motion = legibility, motion
        self._was = theming.manager().text_contrast()
        self._was_motion = motion.intensity()
        theming.manager().set_text_contrast(True)
        motion.set_intensity("lite")
        legibility.reset_caches()
        self.scene = _Scene()

    def tearDown(self) -> None:
        self.scene.close()
        theming.manager().set_text_contrast(self._was)
        self.motion.set_intensity(self._was_motion)
        self.legibility.reset_caches()

    def _go_dark(self) -> None:
        """The whole backdrop turns the dark half's colour at once."""
        bg = self.scene.bg
        dark = QImage(bg._ink_src.size(), QImage.Format_RGB32)
        dark.fill(DARK)
        bg._ink_src = dark
        bg._ink_frame += 1
        bg._ink_grid = None              # skip the probe's own easing

    def _step(self, n: int = 1) -> QColor:
        """``n`` paints 50 ms apart, the last one's ink."""
        lab = self.scene.right
        out = None
        for _ in range(n):
            for st in self.legibility._shown.values():
                st[3] -= 0.05
            out = self.legibility.ink(lab, DIM)
        return out

    def test_new_text_lands_without_a_fade(self) -> None:
        out = self.legibility.ink(self.scene.right, DIM)
        self.assertGreater(out.lightness(), 150)

    def test_a_change_eases_instead_of_jumping(self) -> None:
        lifted = self.legibility.ink(self.scene.right, DIM)
        self._go_dark()
        first = self._step()
        self.assertLess(first.lightness(), lifted.lightness())
        self.assertGreater(first.lightness(), DIM.lightness())
        # Still on its way, so a repaint is booked for the next tick.
        self.assertIn(id(self.scene.right), self.legibility._fading)

    def test_the_fade_lands_on_the_theme_ink_exactly(self) -> None:
        self.legibility.ink(self.scene.right, DIM)
        self._go_dark()
        out = self._step(30)                # 1.5 s of paints
        self.assertEqual(out.name(), DIM.name())
        self.assertEqual(self.legibility._shown, {})

    def test_motion_off_is_instant(self) -> None:
        self.motion.set_intensity("off")
        self.legibility.ink(self.scene.right, DIM)
        self._go_dark()
        self.assertEqual(self._step().name(), DIM.name())


def _brightest_gb(img: QImage, widget: QWidget, root: QWidget) -> int:
    """Highest min(green, blue) under ``widget``: red's sit near 20 and
    the theme's dim tops out at 95, so >150 means light text landed."""
    origin = widget.mapTo(root, widget.rect().topLeft())
    best = 0
    for y in range(origin.y(), origin.y() + widget.height()):
        for x in range(origin.x(), origin.x() + widget.width()):
            c = QColor(img.pixel(x, y))
            best = max(best, min(c.green(), c.blue()))
    return best


class InkLabelTests(unittest.TestCase):
    """Selectable and rich-text labels (lyrics, karaoke) paint through
    Qt's text document, past the proxy; InkLabel sets the ink itself."""

    def setUp(self) -> None:
        from tide.ui import legibility
        _app()
        self.legibility = legibility
        self._was = theming.manager().text_contrast()
        theming.manager().set_text_contrast(True)
        legibility.reset_caches()

        def add_lyric(scene, lay):
            scene.lyric = legibility.InkLabel("a lyric line")
            scene.lyric.setTextInteractionFlags(Qt.TextSelectableByMouse)
            scene.lyric.setStyleSheet(f"color: {DIM.name()}; "
                                      "background: transparent;")
            lay.addWidget(scene.lyric)

        self.scene = _Scene(add_lyric)

    def tearDown(self) -> None:
        self.scene.close()
        theming.manager().set_text_contrast(self._was)
        self.legibility.reset_caches()

    def _settle(self) -> QImage:
        self.scene.bg.grab()             # asks for the move
        _app().processEvents()           # the sheet lands
        return self.scene.bg.grab().toImage()

    def test_selectable_text_reads_on_red(self) -> None:
        lyric = self.scene.lyric
        self.assertGreater(lyric.mapTo(self.scene.bg, lyric.rect().topLeft()).x(),
                           200, "label should sit on red")
        img = self._settle()
        self.assertGreater(_brightest_gb(img, lyric, self.scene.bg), 150)
        # The caller's sheet is kept; the ink rides on the end of it.
        self.assertIn("background: transparent", lyric.styleSheet())

    def test_a_new_sheet_from_the_caller_starts_over(self) -> None:
        lyric = self.scene.lyric
        self._settle()
        lyric.setStyleSheet(f"color: {FG.name()}; background: transparent;")
        self._settle()
        # White already reads on red, so it lands as the caller set it.
        self.assertEqual(lyric._ink_base.name(), FG.name())
        self.assertIsNone(lyric._ink_shown)
        self.assertEqual(lyric.styleSheet(),
                         f"color: {FG.name()}; background: transparent;")

    def test_the_move_comes_off_when_the_setting_does(self) -> None:
        lyric = self.scene.lyric
        self._settle()
        self.assertIsNotNone(lyric._ink_shown)
        theming.manager().set_text_contrast(False)
        self._settle()
        self.assertIsNone(lyric._ink_shown)
        self.assertEqual(lyric.styleSheet(),
                         f"color: {DIM.name()}; background: transparent;")


class StatusBarInkTests(unittest.TestCase):
    def test_message_reads_on_red(self) -> None:
        from tide.ui import legibility
        _app()
        was = theming.manager().text_contrast()
        theming.manager().set_text_contrast(True)
        legibility.reset_caches()

        def add_bar(scene, lay):
            scene.bar = legibility.InkStatusBar()
            scene.bar.setSizeGripEnabled(False)       # as the window has it
            scene.bar.setStyleSheet(f"QStatusBar {{ color: {DIM.name()}; "
                                    "background: transparent; }")
            scene.bar.setFixedWidth(180)
            scene.bar.showMessage("home · paused")
            lay.addWidget(scene.bar)

        scene = _Scene(add_bar)
        try:
            img = scene.bg.grab().toImage()
            bar = scene.bar
            origin = bar.mapTo(scene.bg, bar.rect().topLeft())
            self.assertGreater(origin.x(), 200, "bar should sit on the red half")
            brightest = 0
            for y in range(origin.y(), origin.y() + bar.height()):
                for x in range(origin.x(), origin.x() + bar.width()):
                    c = QColor(img.pixel(x, y))
                    brightest = max(brightest, min(c.green(), c.blue()))
            self.assertGreater(brightest, 150)
        finally:
            scene.close()
            theming.manager().set_text_contrast(was)
            legibility.reset_caches()


class IconInkTests(unittest.TestCase):
    def test_svg_icon_redraws_in_a_readable_ink(self) -> None:
        from tide.ui import legibility
        from tide.ui.widgets import BracketButton
        _app()
        was = theming.manager().text_contrast()
        theming.manager().set_text_contrast(True)
        legibility.reset_caches()
        svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16">'
               '<rect width="16" height="16" fill="currentColor"/></svg>')

        def add_buttons(scene, lay):
            scene.btn = BracketButton("go")
            scene.btn.setSvgIcon(svg)
            lay.addWidget(scene.btn)

        scene = _Scene(add_buttons)
        try:
            btn = scene.btn
            # A dim grey icon, the way a muted modern icon is drawn.
            btn._refresh_svg_icon(svg, ink=DIM.name(), px=16)
            base = btn._icon_spec[1]
            scene.bg.grab()
            origin = btn.mapTo(scene.bg, btn.rect().topLeft())
            self.assertGreater(origin.x(), 200, "button should sit on red")
            self.assertNotEqual(btn._icon_shown, DIM.name())
            self.assertGreater(QColor(btn._icon_shown).lightness(), 150)
            # The theme's ink is remembered, so the icon can go back to it.
            self.assertEqual(base, DIM.name())
        finally:
            scene.close()
            theming.manager().set_text_contrast(was)
            legibility.reset_caches()


if __name__ == "__main__":
    unittest.main()
