"""v2.0 phase 2 — the strip builder: build your player bar.

Pinned: rows come from the real variant registries, never a hardcoded
copy; the miniature preview is built from the real slot factories and
hot-swaps when a combo changes; cancel leaves zero trace; accept emits
overrides_chosen with EXACTLY the minimal diff against the base layout
(returning to a layout default clears its override), and the dialog
itself applies/persists NOTHING — integration owns
update_overrides/apply_layout/save_fields; an untouched accept emits
nothing; open_strip_builder defers construction out of the calling turn
(the modal-from-click crash rule).

Run offscreen:  QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest tests/test_strip_builder.py
"""
import sys
import unittest
from unittest import mock

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QComboBox

from tide import config, theming
from tide import layout as layout_module
from tide import settings as settings_module
from tide.ui import variants


def _app() -> QApplication:
    return QApplication.instance() or QApplication(sys.argv[:1])


class _BuilderCase(unittest.TestCase):
    """Settings writes stubbed shut (the builder must NEVER save), real
    app-wide QSS pushes suppressed, managers pinned to classic/no
    overrides and restored after."""

    def setUp(self) -> None:
        self.app = _app()
        self.saved: list = []
        mock.patch.object(
            settings_module, "save",
            lambda s: self.saved.append("save")).start()
        mock.patch.object(
            settings_module, "save_fields",
            lambda s, *names: self.saved.append(tuple(names))).start()
        mock.patch.object(self.app, "setStyleSheet").start()
        self.addCleanup(mock.patch.stopall)
        theming.manager().refresh()
        layout_module.manager().refresh()
        layout_module.manager().apply("classic", {})

    def tearDown(self) -> None:
        layout_module.manager().apply("classic", {})
        QTest.qWait(20)

    def _builder(self, overrides=None):
        from tide.ui.strip_builder import StripBuilder
        dlg = StripBuilder(overrides=overrides)

        def _cleanup() -> None:
            dlg.deleteLater()
            QTest.qWait(20)

        self.addCleanup(_cleanup)
        return dlg

    def _pick(self, dlg, slot: str, slug: str) -> None:
        combo = dlg.picker_for(slot)
        idx = combo.findData(slug)
        self.assertGreaterEqual(idx, 0, f"{slot}: no {slug!r} row")
        combo.setCurrentIndex(idx)

    def _classic_slots(self) -> dict:
        return dict(layout_module.manager().get("classic").slots)


class GenerationTests(_BuilderCase):
    def test_rows_come_from_the_real_registries(self) -> None:
        dlg = self._builder()
        registry = variants.all_variant_slugs()
        self.assertEqual(set(dlg.pickers), set(registry),
                         "one combo per registry slot, no extras")
        for slot, slugs in registry.items():
            with self.subTest(slot=slot):
                combo = dlg.picker_for(slot)
                self.assertIsInstance(combo, QComboBox)
                rows = [combo.itemData(i) for i in range(combo.count())]
                self.assertEqual(rows, list(slugs),
                                 "rows must be byte-identical to the "
                                 "variants registry")

    def test_initial_state_is_the_effective_layout(self) -> None:
        dlg = self._builder({"progress": "dotted"})
        self.assertEqual(dlg.picker_for("progress").currentData(), "dotted")
        base = self._classic_slots()
        for slot in ("volume", "album_art", "controls", "now_label"):
            self.assertEqual(dlg.picker_for(slot).currentData(), base[slot])

    def test_no_overrides_arg_reads_the_live_manager(self) -> None:
        layout_module.manager().apply("classic", {"volume": "wedge"})
        dlg = self._builder()
        self.assertEqual(dlg.picker_for("volume").currentData(), "wedge")
        self.assertEqual(dlg.picker_for("progress").currentData(), "blocks")

    def test_stale_override_slug_falls_back_to_the_base(self) -> None:
        # An old override naming a variant this version doesn't ship
        # must not leave the combo on an arbitrary row.
        dlg = self._builder({"progress": "spiral"})
        self.assertEqual(dlg.picker_for("progress").currentData(), "blocks")


class PreviewTests(_BuilderCase):
    def test_preview_is_built_from_the_real_factories(self) -> None:
        from tide.ui.widgets import (
            AlbumArt, MonoProgress, MonoVolume, NowPlayingLabel,
        )
        dlg = self._builder()   # classic: blocks/blocks/square/bracket/stacked
        self.assertIsInstance(dlg.preview_widget("progress"), MonoProgress)
        self.assertIsInstance(dlg.preview_widget("volume"), MonoVolume)
        # square/stacked are the base classes exactly — a subclass here
        # would mean the factory resolved the wrong slug.
        self.assertIs(type(dlg.preview_widget("album_art")), AlbumArt)
        self.assertIs(type(dlg.preview_widget("now_label")), NowPlayingLabel)
        self.assertIsInstance(dlg.preview_widget("controls"),
                              variants.ControlsBundle)
        self.assertEqual(dlg.preview_widget("controls").variant, "bracket")

    def test_preview_carries_sample_data(self) -> None:
        dlg = self._builder()
        progress = dlg.preview_widget("progress")
        self.assertGreater(progress._duration, 0,
                           "the preview bar must show a fill, not empty")
        self.assertEqual(dlg.preview_widget("volume").volume(), 65)
        art = dlg.preview_widget("album_art")
        self.assertIsNotNone(art._pixmap_raw,
                             "the preview art must carry a generated cover")

    def test_combo_change_hot_swaps_the_preview(self) -> None:
        dlg = self._builder()
        before = dlg.preview_widget("progress")
        self._pick(dlg, "progress", "bar")
        after = dlg.preview_widget("progress")
        self.assertIsInstance(after, variants.BarProgress)
        self.assertIsNot(after, before)
        self._pick(dlg, "volume", "knob")
        self.assertIsInstance(dlg.preview_widget("volume"),
                              variants.KnobVolume)
        self._pick(dlg, "album_art", "circle")
        self.assertIsInstance(dlg.preview_widget("album_art"),
                              variants.CircleAlbumArt)
        self._pick(dlg, "controls", "large")
        self.assertEqual(dlg.preview_widget("controls").variant, "large")
        QTest.qWait(20)   # drain the deleteLater'd old preview hosts

    def test_preview_never_touches_the_live_manager(self) -> None:
        dlg = self._builder()
        self._pick(dlg, "progress", "dotted")
        self._pick(dlg, "volume", "wedge")
        self.assertEqual(layout_module.manager().current().slots,
                         self._classic_slots(),
                         "browsing variants leaked into the live layout")


class CancelTests(_BuilderCase):
    def test_cancel_leaves_zero_trace(self) -> None:
        caller_overrides = {"progress": "dotted"}
        dlg = self._builder(caller_overrides)
        emitted: list = []
        dlg.overrides_chosen.connect(emitted.append)
        self._pick(dlg, "progress", "bar")
        self._pick(dlg, "controls", "compact")
        dlg.cancel_btn.click()
        self.assertEqual(emitted, [], "cancel must not emit")
        self.assertEqual(self.saved, [], "cancel must not persist")
        self.assertEqual(caller_overrides, {"progress": "dotted"},
                         "the caller's dict was mutated")
        self.assertEqual(layout_module.manager().current().slots,
                         self._classic_slots(),
                         "cancel left a manager push behind")
        self.assertFalse(config.SETTINGS_FILE.exists(),
                         "cancel wrote settings to disk")


class AcceptTests(_BuilderCase):
    def test_accept_emits_exactly_the_minimal_diff(self) -> None:
        dlg = self._builder()
        emitted: list = []
        dlg.overrides_chosen.connect(emitted.append)
        self._pick(dlg, "progress", "dotted")
        self._pick(dlg, "volume", "wedge")
        dlg.accept_btn.click()
        self.assertEqual(emitted, [{"progress": "dotted", "volume": "wedge"}])
        # The dialog itself applied and persisted NOTHING — integration
        # owns update_overrides/apply_layout/save_fields.
        self.assertEqual(self.saved, [])
        self.assertEqual(layout_module.manager().current().slots,
                         self._classic_slots())
        self.assertFalse(config.SETTINGS_FILE.exists())

    def test_returning_to_the_layout_default_emits_empty(self) -> None:
        # The clear-my-override case: the pick changed (dotted → blocks)
        # so the signal MUST fire, and the diff is empty — integration
        # replaces layout_overrides wholesale, wiping the stale entry.
        dlg = self._builder({"progress": "dotted"})
        emitted: list = []
        dlg.overrides_chosen.connect(emitted.append)
        self._pick(dlg, "progress", "blocks")
        dlg.accept_btn.click()
        self.assertEqual(emitted, [{}])

    def test_untouched_accept_emits_nothing(self) -> None:
        dlg = self._builder({"progress": "dotted"})
        emitted: list = []
        dlg.overrides_chosen.connect(emitted.append)
        dlg.accept_btn.click()
        self.assertEqual(emitted, [],
                         "an untouched accept must emit nothing — the "
                         "caller writes nothing")
        self.assertEqual(self.saved, [])

    def test_edit_and_edit_back_emits_nothing(self) -> None:
        dlg = self._builder()
        emitted: list = []
        dlg.overrides_chosen.connect(emitted.append)
        self._pick(dlg, "progress", "thin")
        self._pick(dlg, "progress", "blocks")   # back where it started
        dlg.accept_btn.click()
        self.assertEqual(emitted, [])


class ThemeDefaultsTests(_BuilderCase):
    def setUp(self) -> None:
        super().setUp()
        theming.manager().apply("brutalist-mono")
        QTest.qWait(20)

    def tearDown(self) -> None:
        theming.manager().apply("brutalist-mono")
        super().tearDown()
        QTest.qWait(20)

    def test_reset_lands_on_the_active_themes_slot_prefs(self) -> None:
        theming.manager().apply("nord")   # declares bar/knob/large/inline
        QTest.qWait(20)
        dlg = self._builder({"progress": "dotted"})
        dlg.reset_btn.click()
        theme = theming.manager().current()
        base = self._classic_slots()
        for slot in dlg.pickers:
            with self.subTest(slot=slot):
                expected = theme.slots.get(slot) or base[slot]
                self.assertEqual(dlg.picker_for(slot).currentData(), expected)
        self.assertIsInstance(dlg.preview_widget("progress"),
                              variants.BarProgress)

    def test_reset_stays_inside_the_dialog(self) -> None:
        theming.manager().apply("nord")
        QTest.qWait(20)
        dlg = self._builder()
        emitted: list = []
        dlg.overrides_chosen.connect(emitted.append)
        dlg.reset_btn.click()
        self.assertEqual(emitted, [], "reset must not emit — only accept")
        self.assertEqual(self.saved, [])
        self.assertEqual(layout_module.manager().current().slots,
                         self._classic_slots())

    def test_reset_then_accept_emits_the_theme_diff(self) -> None:
        theming.manager().apply("nord")
        QTest.qWait(20)
        dlg = self._builder()
        emitted: list = []
        dlg.overrides_chosen.connect(emitted.append)
        dlg.reset_btn.click()
        dlg.accept_btn.click()
        theme = theming.manager().current()
        base = self._classic_slots()
        expected = {
            slot: slug for slot, slug in theme.slots.items()
            if slot in base and slug and slug != base[slot]
        }
        self.assertEqual(emitted, [expected])


class DeferredOpenTests(_BuilderCase):
    """open_strip_builder is the sanctioned from-a-click entry point —
    construction must land on its own event-loop turn, never inside the
    calling emission (the modal-from-click segfault rule)."""

    def test_open_defers_construction_out_of_the_calling_turn(self) -> None:
        from tide.ui import strip_builder
        created: list = []
        execd: list = []
        orig_init = strip_builder.StripBuilder.__init__

        def _init(dlg, *args, **kwargs):
            created.append(dlg)
            orig_init(dlg, *args, **kwargs)

        mock.patch.object(
            strip_builder.StripBuilder, "__init__", _init).start()
        mock.patch.object(
            strip_builder.StripBuilder, "exec",
            lambda dlg: execd.append(dlg) or 0).start()
        strip_builder.open_strip_builder(None)
        self.assertEqual(created, [],
                         "the dialog was constructed inside the calling turn")
        QTest.qWait(30)
        self.assertEqual(len(created), 1)
        self.assertEqual(len(execd), 1)
        QTest.qWait(20)   # drain the opener's deleteLater

    def test_open_wires_on_chosen_through_to_the_emission(self) -> None:
        from tide.ui import strip_builder
        got: list = []

        def _exec(dlg) -> int:
            combo = dlg.picker_for("progress")
            combo.setCurrentIndex(combo.findData("dotted"))
            dlg._on_accept()
            return 1

        mock.patch.object(strip_builder.StripBuilder, "exec", _exec).start()
        strip_builder.open_strip_builder(None, on_chosen=got.append)
        self.assertEqual(got, [])
        QTest.qWait(30)
        self.assertEqual(got, [{"progress": "dotted"}])
        self.assertEqual(self.saved, [],
                         "the opener path must not persist either")
        QTest.qWait(20)   # drain the opener's deleteLater


if __name__ == "__main__":
    unittest.main()
