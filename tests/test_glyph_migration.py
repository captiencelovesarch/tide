"""Phase-1 registry call-site migrations — pixel-identical or it didn't
happen.

The glyph/backdrop/heading/status call sites moved off their hand-typed
literals onto the registries (tide.glyphs, tide.backdrops, ui/headings,
theming.status_color). The deal was byte-identical output, so these tests
compare what the widgets actually render against the OLD literals —
spelled as unicode escapes, mirroring test_registries, so a lookalike
codepoint can't sneak through a diff. On top of the equality pins, the
pack-routing tests prove the call sites really go through the registry
(a registered pack changes what a fresh widget draws) rather than having
been re-hardcoded to strings that merely match today.

Run offscreen:  QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest tests/
"""
import sys
import unittest

from PySide6.QtGui import QColor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from tide import glyphs, settings as settings_module, speed_law, theming
from tide.player import PlayState
from tide.playback import MpvBackend, PlaybackRouter
from tide.queue import RepeatMode
from tide.settings import Settings
from tide.sources.local import LocalSource
from tide.ui.headings import line_heading
from tide.ui.window import MainWindow


def _app() -> QApplication:
    return QApplication.instance() or QApplication(sys.argv[:1])


def _window() -> MainWindow:
    router = PlaybackRouter()
    router.register(MpvBackend())
    return MainWindow(LocalSource(), router)


def _destroy_window(w: MainWindow) -> None:
    """close + actually destroy. A merely-closed window leaks its C++
    widget tree (and any parentless mini/fullscreen companions) until GC,
    and every later app-wide restyle repolishes each leaked window — a
    window-heavy suite gets slower with every test that skips this."""
    for companion in (getattr(w, "_mini", None), getattr(w, "_fs", None)):
        if companion is not None:
            companion.close()
            companion.deleteLater()
    w.close()
    w.deleteLater()
    QTest.qWait(30)


# The old hand-typed literals, escape-spelled. If a migration drifted a
# single codepoint, these fail.
PLAY = "\u25b6"                    # ▶
PAUSE = "\u25ae\u25ae"            # ▮▮ — NOT U+23F8
LOADING = "\u2026"                 # …
PREV = "\u25c2\u25c2"             # ◂◂
NEXT = "\u25b8\u25b8"             # ▸▸
SHUFFLE = "\u21cb"                 # ⇋
REPEAT = "\u21bb"                  # ↻
REPEAT_ONE = "\u21bb\u00b9"       # ↻¹
LIKE_ON = "\u2665"                 # ♥
LIKE_OFF = "\u2661"                # ♡
SLEEP = "zzz"
FULLSCREEN = "\u2922"              # ⤢
DASH = "\u2500"                    # ─


class _GlyphStateMixin:
    """Every test that touches the glyph registry must leave it default —
    the registry is process-global."""

    def setUp(self) -> None:
        _app()
        glyphs.set_pack("default")
        glyphs.set_overrides({})

    def tearDown(self) -> None:
        glyphs.set_pack("default")
        glyphs.set_overrides({})


class ControlsBundleGlyphTest(_GlyphStateMixin, unittest.TestCase):
    def test_bundle_carries_the_shipped_literals(self) -> None:
        from tide.ui.variants import ControlsBundle
        for variant in ("bracket", "large", "compact"):
            b = ControlsBundle(variant=variant)
            with self.subTest(variant=variant):
                self.assertEqual(b.shuffle_btn._glyph, SHUFFLE)
                self.assertEqual(b.prev_btn._glyph, PREV)
                self.assertEqual(b.play_btn._glyph, PLAY)
                self.assertEqual(b.next_btn._glyph, NEXT)
                self.assertEqual(b.repeat_btn._glyph, REPEAT)
                # Like is glyph-only: label AND glyph are the open heart.
                self.assertEqual(b.like_btn._glyph, LIKE_OFF)
                self.assertEqual(b.like_btn._label, LIKE_OFF)
                # Word labels stayed words.
                self.assertEqual(b.play_btn._label, "play")
                self.assertEqual(b.prev_btn._label, "prev")

    def test_bundle_routes_through_the_registry(self) -> None:
        from tide.ui.variants import ControlsBundle
        alt = {k: f"<{k}>" for k in glyphs.KEYS}
        glyphs.register_pack("migration-test", alt)
        glyphs.set_pack("migration-test")
        b = ControlsBundle(variant="bracket")
        self.assertEqual(b.play_btn._glyph, "<play>")
        self.assertEqual(b.like_btn._label, "<like_off>")


class WindowTransportGlyphTest(_GlyphStateMixin, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        # Sleep-start persists a preset; never let it near real config.
        self._real_save = settings_module.save
        self._real_save_fields = settings_module.save_fields
        settings_module.save = lambda s: None
        settings_module.save_fields = lambda s, *names: None
        self.w = _window()
        self.w._settings = Settings()

    def tearDown(self) -> None:
        self.w._sleep_cancel(silent=True)
        _destroy_window(self.w)
        settings_module.save = self._real_save
        settings_module.save_fields = self._real_save_fields
        super().tearDown()

    def test_play_button_across_states(self) -> None:
        w = self.w
        w._on_state(PlayState.PLAYING)
        self.assertEqual(w.play_btn._label, "pause")
        self.assertEqual(w.play_btn._glyph, PAUSE)
        w._on_state(PlayState.LOADING)
        self.assertEqual(w.play_btn._label, LOADING)
        self.assertEqual(w.play_btn._glyph, LOADING)
        w._on_state(PlayState.PAUSED)
        self.assertEqual(w.play_btn._label, "play")
        self.assertEqual(w.play_btn._glyph, PLAY)
        w._on_state(PlayState.IDLE)
        self.assertEqual(w.play_btn._glyph, PLAY)

    def test_repeat_button_marks_which_repeat(self) -> None:
        w = self.w
        # Walk the queue to repeat-one, whatever the cycle order is.
        for _ in range(3):
            if w.queue.repeat_mode is RepeatMode.ONE:
                break
            w.queue.cycle_repeat()
        self.assertIs(w.queue.repeat_mode, RepeatMode.ONE)
        w._refresh_mode_buttons()
        self.assertEqual(w.repeat_btn._label, "repeat¹")
        self.assertEqual(w.repeat_btn._glyph, REPEAT_ONE)
        w.queue.cycle_repeat()      # one → off
        w._refresh_mode_buttons()
        self.assertEqual(w.repeat_btn._label, "repeat")
        self.assertEqual(w.repeat_btn._glyph, REPEAT)

    def test_like_button_both_ways(self) -> None:
        w = self.w
        w._liked_current = True
        w._refresh_like_button()
        self.assertEqual(w.like_btn._label, LIKE_ON)
        self.assertEqual(w.like_btn._glyph, LIKE_ON)
        w._liked_current = False
        w._refresh_like_button()
        self.assertEqual(w.like_btn._label, LIKE_OFF)
        self.assertEqual(w.like_btn._glyph, LIKE_OFF)

    def test_sleep_labels(self) -> None:
        from tide.ui.sleep_timer import SleepMode
        w = self.w
        self.assertEqual(w.sleep_btn._label, SLEEP)
        w._sleep_start(SleepMode.MINUTES, 12)
        self.assertEqual(w.sleep_btn._label, "zzz 12m")
        w._sleep_start(SleepMode.AFTER_SONG, 0)
        self.assertEqual(w.sleep_btn._label, "zzz song")
        w._sleep_start(SleepMode.AFTER_QUEUE, 0)
        self.assertEqual(w.sleep_btn._label, "zzz queue")
        w._sleep_cancel(silent=True)
        self.assertEqual(w.sleep_btn._label, SLEEP)

    def test_fullscreen_button_glyph(self) -> None:
        self.assertEqual(self.w.fullscreen_btn._glyph, FULLSCREEN)
        self.assertEqual(self.w.fullscreen_btn._label, "full")


class CompanionWindowGlyphTest(_GlyphStateMixin, unittest.TestCase):
    """Mini + fullscreen player transports render the same vocabulary."""

    def setUp(self) -> None:
        super().setUp()
        self._real_save = settings_module.save
        settings_module.save = lambda s: None
        self.w = _window()
        self.w._settings = Settings()

    def tearDown(self) -> None:
        _destroy_window(self.w)
        settings_module.save = self._real_save
        super().tearDown()

    def _mini(self):
        self.w.set_mini_mode(True)
        return self.w._mini

    def _fs(self):
        self.w.set_fullscreen_mode(True)
        return self.w._fs

    def test_mini_transport(self) -> None:
        m = self._mini()
        self.assertEqual(m.shuffle_btn._glyph, SHUFFLE)
        self.assertEqual(m.prev_btn._glyph, PREV)
        self.assertEqual(m.next_btn._glyph, NEXT)
        self.assertEqual(m.exit_btn._glyph, FULLSCREEN)
        m._on_state(PlayState.PLAYING)
        self.assertEqual(m.play_btn._glyph, PAUSE)
        m._on_state(PlayState.LOADING)
        self.assertEqual(m.play_btn._label, LOADING)
        self.assertEqual(m.play_btn._glyph, LOADING)
        m._on_state(PlayState.PAUSED)
        self.assertEqual(m.play_btn._glyph, PLAY)
        m.set_liked(True)
        self.assertEqual(m.like_btn._glyph, LIKE_ON)
        m.set_liked(False)
        self.assertEqual(m.like_btn._glyph, LIKE_OFF)
        m.set_modes(False, RepeatMode.ONE)
        self.assertEqual(m.repeat_btn._glyph, REPEAT_ONE)
        m.set_modes(False, RepeatMode.ALL)
        self.assertEqual(m.repeat_btn._glyph, REPEAT)

    def test_fullscreen_transport(self) -> None:
        f = self._fs()
        self.assertEqual(f.shuffle_btn._glyph, SHUFFLE)
        self.assertEqual(f.prev_btn._glyph, PREV)
        self.assertEqual(f.next_btn._glyph, NEXT)
        f._on_state(PlayState.PLAYING)
        self.assertEqual(f.play_btn._glyph, PAUSE)
        f._on_state(PlayState.LOADING)
        self.assertEqual(f.play_btn._label, LOADING)
        f._on_state(PlayState.IDLE)
        self.assertEqual(f.play_btn._glyph, PLAY)
        f.set_liked(True)
        self.assertEqual(f.like_btn._glyph, LIKE_ON)
        f.set_modes(True, RepeatMode.ONE)
        self.assertEqual(f.repeat_btn._glyph, REPEAT_ONE)
        f.set_modes(True, RepeatMode.OFF)
        self.assertEqual(f.repeat_btn._glyph, REPEAT)


class HeadingMigrationTest(_GlyphStateMixin, unittest.TestCase):
    @staticmethod
    def _old(label: str, total: int = 60) -> str:
        # The pre-migration builder, verbatim (was copy-pasted 8×).
        styled = theming.styled_case(label)
        line = "─" * max(4, total - len(styled) - 6)
        return f"── {styled} {line}"

    def test_builder_is_byte_identical_to_the_old_copy_paste(self) -> None:
        for label in ("results", "queue", "history · youtube · 42",
                      "your library · loading songs…", "",
                      "x" * 80):
            for total in (60, 40):
                with self.subTest(label=label, total=total):
                    self.assertEqual(line_heading(label, total),
                                     self._old(label, total))

    def test_all_eight_call_sites_share_the_one_builder(self) -> None:
        from tide.ui import album, artist, history, library, lyrics, song_page
        from tide.ui.home import view as home_view
        for mod in (album, artist, history, library, lyrics, song_page,
                    home_view):
            with self.subTest(module=mod.__name__):
                self.assertIs(mod._line_heading, line_heading)
        # The window keeps its method shape but delegates to the builder.
        import inspect
        from tide.ui.window import MainWindow as MW
        src = inspect.getsource(MW._line_heading)
        self.assertIn("line_heading(label, total)", src)
        self.assertNotIn("─", src, "window still builds its own rule")

    def test_headings_ride_the_glyph_pack(self) -> None:
        alt = {k: glyphs.DEFAULT_PACK[k] for k in glyphs.KEYS}
        alt["heading_dash"] = "="
        glyphs.register_pack("migration-heading", alt)
        glyphs.set_pack("migration-heading")
        self.assertTrue(line_heading("results").startswith("== results "))


class BackdropChoicesMigrationTest(unittest.TestCase):
    # The old hand-typed lists, verbatim.
    OLD_MENU = [
        ("follow main", "follow"),
        ("living fields", "field"),
        ("diagonal band", "band"),
        ("bass arch", "vbeam"),
        ("sunset horizon", "horizon"),
        ("lightning", "lightning"),
        ("deep water", "depths"),
        ("rim light", "rimlight"),
        ("liquid cover", "liquid"),
        ("aurora", "aurora"),
        ("smoke", "smoke"),
        ("caustics", "caustics"),
        ("off · flat", "off"),
    ]
    OLD_STYLE_COMBO = [
        ("living fields · layered ambience", "field"),
        ("diagonal band · classic sweep", "band"),
        ("bass arch · hazy hill swells on bass", "vbeam"),
        ("sunset horizon · sun low over water", "horizon"),
        ("lightning · strikes on the beat", "lightning"),
        ("deep water · glow wells up from below", "depths"),
        ("rim light · edges hold the light", "rimlight"),
        ("liquid cover · the album art, melted", "liquid"),
        ("aurora · slow curtains of light", "aurora"),
        ("smoke · drifts, glows from within", "smoke"),
        ("caustics · underwater light web", "caustics"),
    ]

    def test_mini_menu_choices_are_byte_identical(self) -> None:
        from tide.ui import mini
        self.assertEqual(mini._BACKDROP_CHOICES, self.OLD_MENU)

    def test_fullscreen_shares_the_mini_list(self) -> None:
        from tide.ui import fullscreen, mini
        self.assertIs(fullscreen._BACKDROP_CHOICES, mini._BACKDROP_CHOICES)

    def test_settings_dialog_combos_are_byte_identical(self) -> None:
        _app()
        from tide.ui.settings import SettingsDialog
        dlg = SettingsDialog(Settings())
        try:
            style = [(dlg.adaptive_style_picker.itemText(i),
                      dlg.adaptive_style_picker.itemData(i))
                     for i in range(dlg.adaptive_style_picker.count())]
            self.assertEqual(style, self.OLD_STYLE_COMBO)
            mini_rows = [(dlg.mini_backdrop_picker.itemText(i),
                          dlg.mini_backdrop_picker.itemData(i))
                         for i in range(dlg.mini_backdrop_picker.count())]
            self.assertEqual(
                mini_rows,
                [("follow main backdrop style", "follow")]
                + self.OLD_STYLE_COMBO
                + [("off · flat card", "off")],
            )
        finally:
            dlg.deleteLater()

    def test_central_bg_whitelist_comes_from_the_registry(self) -> None:
        from tide import backdrops
        from tide.ui import central_bg
        self.assertEqual(central_bg._STYLES, frozenset(backdrops.SLUGS))
        self.assertEqual(
            central_bg._STYLES,
            central_bg._FX_STYLES | {"field", "band", "liquid"},
        )


class StatusDotMigrationTest(unittest.TestCase):
    """The source-panel dot now paints theming.status_color; with no theme
    tokens in play the fallbacks are the exact old hardcodes."""

    def _dot_pixel(self, state: str) -> QColor:
        _app()
        from tide.ui.source_panel import _StatusDot
        dot = _StatusDot()
        dot.set_state(state)
        img = dot.grab().toImage()
        return QColor(img.pixelColor(dot.SIZE // 2, dot.SIZE // 2))

    def test_ok_warn_ride_status_color(self) -> None:
        for state in ("ok", "warn"):
            with self.subTest(state=state):
                self.assertEqual(
                    self._dot_pixel(state),
                    QColor(theming.status_color(state)),
                )

    def test_off_keeps_the_disabled_neutral(self) -> None:
        self.assertEqual(self._dot_pixel("off"), QColor("#555"))

    def test_fallbacks_are_the_old_hardcodes(self) -> None:
        # The dark fallback family IS the palette source_panel hand-typed
        # before tokens existed — dark-theme users see zero change.
        self.assertEqual(theming._STATUS_FALLBACKS_DARK["ok"], "#5aaf6a")
        self.assertEqual(theming._STATUS_FALLBACKS_DARK["warn"], "#d4b95e")


class SpeedMigrationTest(unittest.TestCase):
    def test_mpris_rate_window_comes_from_speed_law(self) -> None:
        import tide.mpris as mpris
        self.assertEqual(mpris.SPEED_MIN, speed_law.SPEED_MIN)
        self.assertEqual(mpris.SPEED_MAX, speed_law.SPEED_MAX)
        # Same values the old guarded-import fallback hardcoded.
        self.assertEqual((mpris.SPEED_MIN, mpris.SPEED_MAX), (0.5, 2.0))


if __name__ == "__main__":
    unittest.main()
