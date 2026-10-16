"""2.3 bar customization — the editor, the long-press, the popover, the
window applying it live and saving it field-scoped, and the settings row.

Run offscreen:  QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest tests/test_bar_customizer.py
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from tide import config, presets, theming
from tide import layout as layout_module
from tide import settings as settings_module
from tide.playback import MpvBackend, PlaybackRouter
from tide.settings import Settings
from tide.sources.local import LocalSource
from tide.ui import bar_config, motion
from tide.ui.bar_customizer import BarCustomizerPopover, BarEditor


def _app() -> QApplication:
    return QApplication.instance() or QApplication(sys.argv[:1])


class EditorTests(unittest.TestCase):
    def setUp(self) -> None:
        _app()
        self.got: list[dict] = []
        self.ed = BarEditor({}, "stacked")
        self.ed.changed.connect(self.got.append)

    def tearDown(self) -> None:
        self.ed.deleteLater()
        QTest.qWait(10)

    def test_unticking_hides(self) -> None:
        self.ed.controls.row("repeat").check.setChecked(False)
        self.assertEqual(self.got[-1]["hidden"], ["repeat"])

    def test_play_box_is_locked(self) -> None:
        self.assertFalse(self.ed.controls.row("play").check.isEnabled())

    def test_size_buttons(self) -> None:
        self.ed.controls.row("volume").sizes.button("s").click()
        self.assertEqual(self.got[-1]["sizes"], {"volume": "s"})

    def test_drag_reorder_emits_new_order(self) -> None:
        self.ed.controls.move_key("fullscreen", 0)
        self.assertEqual(self.got[-1]["order"][0], "fullscreen")

    def test_alt_arrow_nudges(self) -> None:
        self.ed._nudge("next", -1)
        order = self.got[-1]["order"]
        self.assertLess(order.index("next"), order.index("play"))

    def test_chip_toggle_takes_over_the_line(self) -> None:
        self.ed._toggle_field("line1", "artist")      # stacked: artist, title
        self.assertEqual(self.got[-1]["line1"], ["title"])
        self.ed._toggle_field("line1", "album")
        self.assertEqual(self.got[-1]["line1"], ["title", "album"])

    def test_chip_drag_reorders_active_fields(self) -> None:
        self.ed._move_chip("line1", "title", 0)
        self.assertEqual(self.got[-1]["line1"], ["title", "artist"])

    def test_style_own_returns_to_variant(self) -> None:
        self.ed._toggle_field("line2", "album")
        self.ed.line2_own.click()
        self.assertIsNone(bar_config.normalize(self.got[-1])["line2"])

    def test_layout_page_and_reset(self) -> None:
        self.ed.layout_pickers["time"].button("remaining").click()
        self.assertEqual(self.got[-1]["time"], "remaining")
        self.ed.reset_btn.click()
        self.assertEqual(self.got[-1], {})     # the storage form of default

    def test_no_emit_on_noop(self) -> None:
        self.ed.layout_pickers["art"].button("m").click()   # already m
        self.assertEqual(self.got, [])

    def test_flow_drop_index(self) -> None:
        self.ed._show_page("info")
        self.ed.resize(420, 400)
        self.ed.show()
        QTest.qWait(20)
        chips = self.ed.line1_chips
        first = chips.row(chips.keys()[0]).geometry()
        self.assertEqual(chips.drop_index_at(QPoint(first.left() + 1, first.center().y())), 0)
        self.assertEqual(chips.drop_index_at(QPoint(9999, 9999)), len(chips.keys()))


class _WindowCase(unittest.TestCase):
    def setUp(self) -> None:
        self.app = _app()
        self._tmp = tempfile.TemporaryDirectory(prefix="tide-bar-")
        self.addCleanup(self._tmp.cleanup)
        self._orig = config.SETTINGS_FILE
        config.SETTINGS_FILE = Path(self._tmp.name) / "settings.toml"
        self.addCleanup(setattr, config, "SETTINGS_FILE", self._orig)
        theming.manager().refresh()
        layout_module.manager().refresh()
        mock.patch.object(self.app, "setStyleSheet").start()
        self.addCleanup(mock.patch.stopall)
        self._prev_motion = motion._user_intensity
        motion.set_intensity(motion.Intensity.OFF)
        self.addCleanup(motion.set_intensity, self._prev_motion)
        self.s = Settings()
        presets.apply_preset(self.s, "brutalist", persist=False)
        from tide.ui.window import MainWindow
        router = PlaybackRouter()
        router.register(MpvBackend())
        self.w = MainWindow(LocalSource(), router)
        self.w._settings = self.s
        self.w.resize(1100, 720)
        self.w.show()
        QTest.qWait(20)

    def tearDown(self) -> None:
        pop = getattr(self.w, "_bar_popover", None)
        if pop is not None:
            try:
                pop.close()
            except RuntimeError:
                pass
        try:
            theming.manager().theme_changed.disconnect(self.w._on_theme_changed)
        except (RuntimeError, TypeError):
            pass
        self.w.close()
        self.w.deleteLater()
        QTest.qWait(30)


class WindowBarTests(_WindowCase):
    def test_default_bar_shows_everything(self) -> None:
        for item in bar_config.ITEMS:
            self.assertTrue(self.w._bar_widget(item).isVisibleTo(self.w.strip), item)

    def test_hide_show_live(self) -> None:
        self.w.set_bar_layout(bar_config.set_shown({}, "sleep", False), persist=False)
        self.assertFalse(self.w.sleep_btn.isVisibleTo(self.w.strip))
        self.w.set_bar_layout({}, persist=False)
        self.assertTrue(self.w.sleep_btn.isVisibleTo(self.w.strip))

    def test_reorder_rebuilds_rows(self) -> None:
        self.w.set_bar_layout(bar_config.move({}, "volume", 0), persist=False)
        left, _right = self.w._bar_rows()
        self.assertIs(left[0], self.w.volume)
        # every bar widget survived the rebuild, parented to the strip
        for item in bar_config.ITEMS:
            self.assertIs(self.w._bar_widget(item).parentWidget(), self.w.strip)

    def test_size_steps_reach_widgets(self) -> None:
        cfg = bar_config.set_size(bar_config.set_size({}, "play", "l"), "volume", "s")
        base_w = self.w.volume.minimumWidth()
        self.w.set_bar_layout(cfg, persist=False)
        self.assertAlmostEqual(self.w.play_btn.sizeFactor(), bar_config.SIZE_FACTORS["l"])
        self.assertLess(self.w.volume.minimumWidth(), base_w)
        self.w.set_bar_layout({}, persist=False)
        self.assertEqual(self.w.volume.minimumWidth(), base_w)
        self.assertAlmostEqual(self.w.play_btn.sizeFactor(), 1.0)

    def test_time_and_art(self) -> None:
        self.w._update_time_label(42, 190)
        self.w.set_bar_layout({"time": "remaining", "art": "off"}, persist=False)
        self.assertEqual(self.w.time_label.text(), "−2:28")
        self.assertFalse(self.w.art.isVisibleTo(self.w.strip))
        self.w.set_bar_layout({"time": "off"}, persist=False)
        self.assertFalse(self.w.time_label.isVisibleTo(self.w.strip))

    def test_info_lines_follow_config(self) -> None:
        lbl = self.w.now_label
        lbl.setTrack("Daft Punk", "Instant Crush", "RAM")
        lbl.setInsightParts({"plays": "412m plays", "likes": "", "year": "2013"})
        lbl.setSource("youtube music")
        sc = lambda t: theming.styled_case(t, lbl._theme)  # noqa: E731
        # stacked's own lines: the pre-2.3 text, punctuation and all
        self.assertEqual(lbl._lines(), (
            sc("Daft Punk — Instant Crush"),
            sc("RAM") + "  ·  " + sc("412m plays · 2013")))
        self.w.set_bar_layout({"line1": ["title"], "line2": ["year", "source"]},
                              persist=False)
        l1, l2 = lbl._lines()
        self.assertEqual(l1, sc("Instant Crush"))
        self.assertEqual(l2, sc("2013") + "  ·  " + sc("youtube music"))

    def test_next_off_hides_up_next(self) -> None:
        self.w.set_bar_layout({"next": "off"}, persist=False)
        self.w._refresh_up_next()
        self.assertFalse(self.w.up_next.isVisibleTo(self.w.strip))

    def test_next_below_moves_it(self) -> None:
        col = self.w._strip_right_col
        self.w.set_bar_layout({"next": "below"}, persist=False)
        self.assertEqual(col.indexOf(self.w.up_next), 1)
        self.w.set_bar_layout({"next": "above"}, persist=False)
        self.assertEqual(col.indexOf(self.w.up_next), 0)

    def test_persists_field_scoped_and_default_as_empty(self) -> None:
        self.w.set_bar_layout(bar_config.set_shown({}, "repeat", False))
        on_disk = settings_module.load()
        self.assertEqual(on_disk.bar_layout["hidden"], ["repeat"])
        self.w.set_bar_layout({})
        self.assertEqual(settings_module.load().bar_layout, {})

    def test_settings_applier(self) -> None:
        self.s.bar_layout = {"hidden": ["shuffle"]}
        self.w.apply_bar_layout_setting()
        self.assertFalse(self.w.shuffle_btn.isVisibleTo(self.w.strip))

    def test_bar_is_per_personality(self) -> None:
        self.w.set_bar_layout({"hidden": ["shuffle"]}, persist=False)
        presets.apply_preset(self.s, "modern", window=self.w, persist=False)
        self.assertTrue(self.w.shuffle_btn.isVisibleTo(self.w.strip))
        presets.apply_preset(self.s, "brutalist", window=self.w, persist=False)
        self.assertFalse(self.w.shuffle_btn.isVisibleTo(self.w.strip))

    def test_popover_opens_and_edits_live(self) -> None:
        self.w.open_bar_customizer(self.w.strip.mapToGlobal(QPoint(500, 40)))
        QTest.qWait(30)
        pop = self.w._bar_popover
        self.assertIsInstance(pop, BarCustomizerPopover)
        self.assertTrue(pop.isVisible())
        pop.editor.controls.row("like").check.setChecked(False)
        self.assertFalse(self.w.like_btn.isVisibleTo(self.w.strip))


class LongPressTests(_WindowCase):
    def _empty_point(self) -> QPoint:
        # the gap between the transport cluster and the right cluster
        left = self.w.like_btn.geometry()
        right = self.w.audio_fx_btn.geometry()
        return QPoint((left.right() + right.left()) // 2, left.center().y())

    def test_press_on_empty_space_is_empty(self) -> None:
        lp = self.w._bar_press
        self.assertTrue(lp.is_empty_at(self._empty_point()))
        self.assertTrue(lp.is_empty_at(self.w.time_label.geometry().center()))

    def test_controls_and_title_are_not_empty(self) -> None:
        lp = self.w._bar_press
        self.assertFalse(lp.is_empty_at(self.w.play_btn.geometry().center()))
        self.assertFalse(lp.is_empty_at(self.w.now_label.geometry().center()))
        self.assertFalse(lp.is_empty_at(self.w.art.geometry().center()))

    def test_hold_fires_and_release_cancels(self) -> None:
        lp = self.w._bar_press
        fired: list = []
        lp.triggered.connect(fired.append)
        with mock.patch.object(self.w, "open_bar_customizer"):
            pt = self._empty_point()
            QTest.mousePress(self.w.strip, Qt.LeftButton, Qt.NoModifier, pt)
            QTest.mouseRelease(self.w.strip, Qt.LeftButton, Qt.NoModifier, pt)
            QTest.qWait(600)
            self.assertEqual(fired, [])
            QTest.mousePress(self.w.strip, Qt.LeftButton, Qt.NoModifier, pt)
            QTest.qWait(650)
            QTest.mouseRelease(self.w.strip, Qt.LeftButton, Qt.NoModifier, pt)
        self.assertEqual(len(fired), 1)


class SettingsRowTests(unittest.TestCase):
    def setUp(self) -> None:
        _app()
        self._real = settings_module.save_fields
        self.saved: list = []
        settings_module.save_fields = lambda s, *n: self.saved.append(n)

    def tearDown(self) -> None:
        settings_module.save_fields = self._real
        QTest.qWait(10)

    def test_untouched_rows_are_not_a_diff(self) -> None:
        from tide.ui.settings import SettingsDialog
        dlg = SettingsDialog(Settings())
        dlg._on_save()
        for key in ("bar_layout", "font_roles", "icon_set"):
            self.assertNotIn(key, dlg.changed_keys())

    def test_bar_editor_edit_saves(self) -> None:
        from tide.ui.settings import SettingsDialog
        s = Settings()
        dlg = SettingsDialog(s)
        ed = dlg.widget_for("bar_layout")
        self.assertIsInstance(ed, BarEditor)
        ed.controls.row("repeat").check.setChecked(False)
        dlg._on_save()
        self.assertIn("bar_layout", dlg.changed_keys())
        self.assertEqual(s.bar_layout["hidden"], ["repeat"])


if __name__ == "__main__":
    unittest.main()
