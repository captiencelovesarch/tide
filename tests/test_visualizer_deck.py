"""the 2.3 visualizer: scene state, the beat deck's map reading, the
canvas across every scene/look/flash, the view's settings plumbing, and
the ambient controller's watch-only beat clock.

Run offscreen:  QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest tests/test_visualizer_deck.py
"""
import sys
import time
from types import SimpleNamespace

import numpy as np
import pytest
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QColor, QImage, QRegion
from PySide6.QtWidgets import QApplication, QLabel, QWidget

from tide import settings as settings_module
from tide.beat_map import Beat, BeatMap, Hit
from tide.player import PlayState
from tide.ui import vis_deck
from tide.ui.ambient import BeatClock
from tide.ui.vis_deck import (
    LiveTrace, MapView, Spectrum, VisEngine, _perim_param, _perim_point,
    _perim_polyline, resolve_look,
)
from tide.ui import vis_play
from tide.ui.vis_play import LAYOUTS, MissingTile, TileGrid
from tide.ui.visualizer import VisualizerView, renderer_slugs
from test_pulse_clock import h  # noqa: F401  shared player/capture clock harness
from test_beat_ambient import KEY, beat_map as flat_beat_map


@pytest.fixture(scope="module", autouse=True)
def _app():
    return QApplication.instance() or QApplication(sys.argv[:1])


PERIOD = 0.5


def song(duration=40.0, complete=True, confidence=0.9, hits=()):
    beats = tuple(Beat(i * PERIOD, 1.0 if i % 4 == 0 else 0.6, i % 4, PERIOD, True)
                  for i in range(int(duration / PERIOD)))
    swell = tuple(min(1.0, i / 80.0) for i in range(int(duration * 4) + 2))
    return BeatMap(duration, complete, 120.0, confidence, beats, tuple(hits), swell)


def render(widget, w=900, h=560):
    widget.resize(w, h)
    img = QImage(w, h, QImage.Format_ARGB32_Premultiplied)
    img.fill(QColor("#101010"))
    widget.render(img, QPoint(), QRegion(), QWidget.RenderFlag.DrawChildren)
    return img


# ---------- spectrum ----------

def test_spectrum_resamples_eases_and_holds_peaks():
    s = Spectrum(48)
    s.push(np.full(32, 0.8))
    assert s._target.shape == (48,)
    s.step(0.016, 0.0)
    assert 0.0 < s.level.max() < 0.8          # eased, not stepped
    for k in range(1, 30):
        s.step(0.016, k * 0.016)
    assert s.level.min() == pytest.approx(0.8, abs=0.01)
    s.silence()
    s.step(0.05, 0.6)
    assert s.level.max() < 0.8
    assert s.peak.max() == pytest.approx(0.8, abs=0.01)   # held
    t = 0.6
    for _ in range(120):
        t += 0.016
        s.step(0.016, t)
    assert s.peak.max() < 0.05                 # and fell
    assert s.settled()


def test_spectrum_ignores_garbage_frames():
    s = Spectrum(16)
    s.push(np.array([np.nan, 2.0, -1.0, 0.5]))
    assert np.all((s._target >= 0) & (s._target <= 1))
    s.push(np.zeros((2, 2)))                   # wrong shape: kept the last one
    assert s._target.shape == (16,)


# ---------- the map, as the deck reads it ----------

def test_beat_info_counts_bars_and_phases():
    mv = MapView(song())
    info = mv.info(0.1)
    assert info.trusted and info.bpm == pytest.approx(120.0)
    assert (info.phase, info.bar) == (0, 1)
    assert mv.info(0.6).phase == 1
    info = mv.info(2.05)                       # beat 4 = phase 0 of bar 2
    assert (info.phase, info.bar) == (0, 2)
    assert mv.info(-1.0).phase is None


def test_an_untrusted_grid_shows_no_tempo():
    mv = MapView(song(confidence=0.1))
    assert not mv.trusted
    assert mv.info(3.0).bpm is None


def test_kick_peaks_on_the_beat_and_flash_rides_downbeats_only():
    mv = MapView(song())
    on = mv.kick(30.0)                         # full swell: full depth
    between = mv.kick(30.0 + PERIOD * 0.5)
    assert on > 0.8 and between < on * 0.5
    assert mv.kick(2.0) < on                   # a quiet stretch hits softer
    assert mv.flash(2.01) > 0.8                # a downbeat
    assert mv.flash(2.51) == 0.0               # phase 1 never flashes
    assert mv.flash(2.0 + vis_deck.FLASH_REACH_S + 0.01) == 0.0


def test_big_hits_flash_and_ring():
    mv = MapView(song(hits=(Hit(2.75, 0.9), Hit(3.75, 0.4))))
    assert mv.flash(2.76) > 0.8
    assert mv.flash(3.76) == 0.0               # a small hit does not
    rings = mv.rings(2.8)
    assert any(abs(age - 0.05) < 1e-6 and s == pytest.approx(0.9) for age, s in rings)
    assert all(0 <= age < vis_deck.RING_S for age, _s in rings)


def test_swell_window_clamps_to_the_map():
    mv = MapView(song(duration=10.0))
    t, v = mv.swell_between(-5.0, 2.0)
    assert t[0] == 0.0 and t[-1] == pytest.approx(2.0)
    t, v = mv.swell_between(9.0, 30.0)
    assert t[-1] <= 10.5


def test_live_trace_edges_rearm_below_threshold():
    live = LiveTrace()
    for i, level in enumerate((0.1, 0.7, 0.9, 0.7, 0.2, 0.8)):
        live.push(100.0 + i * 0.05, level)
    assert len(live.edges) == 2                # rose twice, held once
    assert live.rings(100.3)
    live.clear()
    assert not live.samples and not live.edges


def test_auto_look_follows_the_theme_aesthetic():
    assert resolve_look("auto", SimpleNamespace(aesthetic="modern")) == "glow"
    assert resolve_look("auto", SimpleNamespace(aesthetic="brutalist")) == "blocks"
    assert resolve_look("auto", None) == "blocks"
    assert resolve_look("glow", SimpleNamespace(aesthetic="brutalist")) == "glow"


# ---------- the engine and the tiles ----------

class _Player:
    def __init__(self):
        from PySide6.QtCore import QObject, Signal

        class S(QObject):
            state_changed = Signal(object)
            position_changed = Signal(float)
            duration_changed = Signal(float)
        self.sig = S()
        self.state_changed = self.sig.state_changed
        self.position_changed = self.sig.position_changed
        self.duration_changed = self.sig.duration_changed
        self.state = PlayState.PLAYING
        self.seeks = []

    def seek(self, seconds):
        self.seeks.append(seconds)


class _Ambient:
    def __init__(self, clock=None):
        self.clock = clock
        self.watching = []

    def set_watching(self, name, on):
        self.watching.append((name, on))

    def beat_clock(self):
        return self.clock


def _settings(**kw):
    s = settings_module.Settings()
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def _view(layout="quad", tiles=("spectrum", "deck", "lyrics", "scope"), clock=None, **kw):
    s = _settings(visualizer_layout=layout, visualizer_tile_1=tiles[0],
                  visualizer_tile_2=tiles[1], visualizer_tile_3=tiles[2],
                  visualizer_tile_4=tiles[3], **kw)
    amb = _Ambient(clock)
    v = VisualizerView()
    v.attach(settings=lambda: s, ambient=lambda: amb)
    v.resize(1000, 640)
    return v, s, amb


def _feed(e, playing=True):
    e.set_now_playing(SimpleNamespace(title="like him", artists="tyler, the creator"))
    e.set_duration(40.0)
    e.set_playing(playing)
    e.update_bands(np.linspace(0.9, 0.1, 32))
    e.update_waveform(np.sin(np.linspace(0, 30, 1024)).astype(np.float32))
    e.update_pulse(0.9)


def _engine(clock=None, **kw):
    e = VisEngine()
    e.apply_settings(_settings(**kw))
    e.set_clock_provider(lambda: clock[0] if isinstance(clock, list) else clock)
    return e


# the river, spectrogram, halo and tapper are the vis-tiles plugin's now;
# tide-plugins' tests/test_vis_tiles.py paints them
TILE_SETS = (("spectrum", "mirror", "scope", "classic"),
             ("deck", "lyrics", "spectrum", "mirror"))


@pytest.mark.parametrize("kinds", TILE_SETS)
@pytest.mark.parametrize("look", ("blocks", "glow"))
def test_every_tile_paints_in_both_looks(kinds, look, monkeypatch):
    monkeypatch.setattr(settings_module, "save_fields", lambda *a, **k: None)
    clock = [BeatClock(song(hits=(Hit(4.25, 0.9),)), 3.9, 1.0)]
    v, s, amb = _view(tiles=kinds, clock=None, visualizer_look=look)
    amb.clock = clock[0]
    v.show()
    e = v.engine
    _feed(e)
    assert [t.kind for t in v.tiles()] == list(kinds)
    for flash in ("off", "soft", "hard"):
        s.visualizer_flash = flash
        v.apply_settings(s)
        for k in range(6):
            amb.clock = BeatClock(clock[0].beat_map, 3.9 + k * 0.05, 1.0)
            e.tick()
        render(v)
    assert e.look == look
    v.teardown()


def test_tiles_paint_without_a_map_with_a_partial_map_and_tiny():
    v, s, amb = _view(tiles=("spectrum", "mirror", "deck", "scope"))
    v.show()
    e = v.engine
    _feed(e)
    e.tick()
    render(v)
    amb.clock = BeatClock(song(duration=12.0, complete=False), 10.0, 1.0)
    e.tick()
    render(v)
    render(v, 200, 160)
    v.teardown()


def test_layouts_keep_a_tile_whose_kind_stays():
    v, s, _amb = _view(layout="stack", tiles=("spectrum", "deck", "lyrics", "mirror"))
    assert [t.kind for t in v.tiles()] == ["spectrum", "deck"]
    deck = v.tiles()[1]
    s.visualizer_layout = "quad"
    v.apply_settings(s)
    tiles = v.tiles()
    assert [t.kind for t in tiles] == ["spectrum", "deck", "lyrics", "mirror"]
    assert tiles[1] is deck                     # kept, not rebuilt
    assert [t.caption_here for t in tiles] == [True, False, False, False]
    s.visualizer_layout = "single"
    v.apply_settings(s)
    assert len(v.tiles()) == 1 and v.tiles()[0].solo
    for name, slots in LAYOUTS.items():
        s.visualizer_layout = name
        v.apply_settings(s)
        assert len(v.tiles()) == len(slots)
    v.teardown()


def test_kicks_leave_trails_and_ripple_the_outlines():
    clock = [BeatClock(song(), 1.95, 1.0)]
    e = _engine(clock)
    _feed(e)
    e.tick()
    assert not e.frame.ghosts and not e.frame.ripples
    clock[0] = BeatClock(clock[0].beat_map, 2.02, 1.0)    # over the beat at 2.0
    e.tick()
    assert e.kicks_now == [1.0]
    assert len(e.frame.ghosts) == 1 and len(e.frame.ripples) == 1
    clock[0] = BeatClock(clock[0].beat_map, 9.0, 1.0)     # a seek: no backlog
    e.tick()
    assert e.kicks_now == []
    e.apply_settings(_settings(visualizer_trails=False, visualizer_ripple=False))
    clock[0] = BeatClock(clock[0].beat_map, 9.3, 1.0)
    e.tick()
    clock[0] = BeatClock(clock[0].beat_map, 9.52, 1.0)
    e.tick()
    assert e.kicks_now and not e.frame.ghosts and not e.frame.ripples


def test_outline_geometry():
    from PySide6.QtCore import QRectF
    r = QRectF(0, 0, 100, 50)
    assert _perim_point(r, 0).toTuple() == (0, 0)
    assert _perim_point(r, 150).toTuple() == (100, 50)
    assert _perim_point(r, 250).toTuple() == (0, 50)
    assert _perim_point(r, 300).toTuple() == (0, 0)       # wraps
    assert _perim_param(r, QPointF(100, 25)) == 125
    assert _perim_param(r, QPointF(50, 49)) == 200
    pts = [q.toTuple() for q in _perim_polyline(r, 90, 110)]
    assert pts == [(90, 0), (100, 0), (100, 10)]          # the corner is a vertex
    pts = [q.toTuple() for q in _perim_polyline(r, -10, 10)]
    assert pts == [(0, 10), (0, 0), (10, 0)]


def test_the_deck_tile_seeks():
    from PySide6.QtTest import QTest
    v, _s, amb = _view(layout="single", tiles=("deck", "deck", "deck", "deck"),
                       clock=BeatClock(song(), 10.0, 1.0))
    v.show()
    e = v.engine
    _feed(e)
    e.tick()
    deck = v.tiles()[0]
    render(v)
    strip = deck.strip_rect
    assert strip is not None
    seen = []
    e.seek_requested.connect(seen.append)
    QTest.mouseClick(deck, Qt.LeftButton, Qt.NoModifier,
                     QPointF(strip.left() + strip.width() * 0.5, strip.center().y()).toPoint())
    assert seen and seen[0] == pytest.approx(20.0, abs=0.5)
    v.teardown()


def test_engine_idles_when_paused_and_settled_and_wakes_on_data():
    e = _engine()
    e.set_active(True)
    assert e.is_animating()
    e.set_playing(False)
    e.spec.resize(e.spec.n)
    e.tick()
    assert not e.is_animating()
    e.update_bands(np.full(32, 0.5))
    assert e.is_animating()
    e.set_active(False)
    assert not e.is_animating()


def test_expensive_frames_back_off_to_30_and_recover():
    e = _engine()
    for _ in range(40):
        e.account(20.0)
        e._settle_paint_cost()
    assert e.effective_fps() == 30 and e._timer.interval() == 33
    for _ in range(10):
        e.account(7.0)
        e._settle_paint_cost()
    assert e.effective_fps() == 30              # between the marks: hold
    for _ in range(60):
        e.account(1.0)
        e._settle_paint_cost()
    assert e.effective_fps() == 60 and e._timer.interval() == 16
    e.apply_settings(_settings(visualizer_fps=30))
    for _ in range(40):
        e.account(1.0)
        e._settle_paint_cost()
    assert e.effective_fps() == 30              # a 30 pick stays 30


# ---------- the play tiles ----------

def _loud_song(duration=40.0):
    base = song(duration)
    n = int(duration * 16) + 1
    loud = tuple(min(1.0, 0.2 + 0.7 * (i / n)) for i in range(n))     # a long build
    return BeatMap(base.duration, base.complete, base.tempo, base.confidence,
                   base.beats, base.hits, base.swell, base.swell_rate, loud, 16.0)


def test_map_view_reads_loudness_and_falls_back_to_the_swell():
    mv = MapView(_loud_song())
    assert mv.loud_rate == 16.0
    t, v = mv.loud_between(10.0, 11.0)
    assert t[0] == pytest.approx(10.0) and len(t) == 17
    assert v[-1] > v[0]                          # the build
    old = MapView(song())                        # a map cached before loudness
    assert old.loud_rate == old.swell_rate and old.loud.size == old.swell.size


def test_art_colours_are_the_covers_in_glow_and_the_theme_in_blocks():
    from PySide6.QtGui import QColor as C
    art = QImage(64, 64, QImage.Format_RGB32)
    art.fill(C("#d03a2a"))
    for y in range(32, 64):
        for x in range(64):
            art.setPixelColor(x, y, C("#2a4ad0"))
    e = _engine(visualizer_look="glow")
    e.set_now_playing(SimpleNamespace(title="t", artists="a", thumbnail="https://x/cover.jpg"))
    e._art_asked = True                           # no network in tests
    e.set_art("https://x/cover.jpg", art)
    primary, secondary, _shadow = e.colors()
    hues = sorted(round(c.hueF(), 1) for c in (primary, secondary))
    assert hues[0] == pytest.approx(0.0, abs=0.06)          # the red
    assert 0.55 <= hues[1] <= 0.7                           # the blue
    e.set_art("https://other/cover.jpg", art)    # a late fetch for another song
    e2 = _engine(visualizer_look="blocks")
    e2.art_cols = e.art_cols
    assert e2.colors()[0] == e2.pal.accent


def test_art_colours_skip_a_grey_cover():
    from tide.ui.vis_deck import art_colors
    from PySide6.QtGui import QColor as C
    grey = QImage(32, 32, QImage.Format_RGB32)
    grey.fill(C("#808080"))
    assert art_colors(grey) is None


def test_saved_layouts_with_retired_tiles_migrate(tmp_path, monkeypatch):
    from tide import config
    path = tmp_path / "settings.toml"
    monkeypatch.setattr(config, "SETTINGS_FILE", path)
    path.write_text(
        'visualizer_layout = "quad"\n'
        'visualizer_tile_1 = "life"\n'
        'visualizer_tile_2 = "deck"\n'
        'visualizer_tile_3 = "particles"\n'
        'visualizer_tile_4 = "pond"\n'
        'visualizer_pond_speed = 3\n'
        'visualizer_life_seed = "heavy"\n', encoding="utf-8")
    s = settings_module.load()
    assert (s.visualizer_tile_1, s.visualizer_tile_2, s.visualizer_tile_3,
            s.visualizer_tile_4) == ("spectrogram", "deck", "river", "halo")
    assert not hasattr(s, "visualizer_pond_speed")
    # the halo is a store tile since 3.0: a fresh tide's fourth slot is core's
    assert settings_module.Settings().visualizer_tile_4 == "scope"


# ---------- the view ----------

def test_menus_build_and_write_the_settings(monkeypatch):
    saved = []
    monkeypatch.setattr(settings_module, "save_fields",
                        lambda s, *names: saved.extend(names))
    v, s, _amb = _view()
    menu = v._build_style_menu()
    tile_menu = v._build_tile_menu(3)
    from PySide6.QtGui import QAction
    actions = {a.text(): a for a in tile_menu.findChildren(QAction)}
    actions["mirror"].trigger()
    assert s.visualizer_tile_4 == "mirror" and "visualizer_tile_4" in saved
    assert v.tiles()[3].kind == "mirror"
    knob_menu = v._build_tile_menu(3)
    texts = [a.text() for a in knob_menu.findChildren(QAction)]
    assert "64" in texts                          # the mirror's bars knob
    menu.deleteLater()
    tile_menu.deleteLater()
    knob_menu.deleteLater()
    v.teardown()


def test_view_routes_seek_and_watches_while_shown():
    player = _Player()
    ambient = _Ambient()
    v = VisualizerView()
    v.attach(player=player, settings=lambda: _settings(), ambient=lambda: ambient)
    v.engine.seek_requested.emit(12.5)
    assert player.seeks == [12.5]
    v.show()
    assert ambient.watching[-1] == ("visualizer", True)
    v.hide()
    v._release_if_hidden()
    assert ambient.watching[-1] == ("visualizer", False)
    v.show()
    v.teardown()
    assert ambient.watching[-1] == ("visualizer", False)


def test_fullscreen_hides_the_header_and_esc_brings_it_back(monkeypatch):
    from PySide6.QtTest import QTest
    v, _s, _amb = _view()
    v.show()
    win = v.window()
    monkeypatch.setattr(win, "showFullScreen", lambda: None)
    monkeypatch.setattr(win, "showNormal", lambda: None)
    v._toggle_fullscreen()
    assert v._fullscreen and v._header.isHidden()
    QTest.keyClick(v._grid, Qt.Key_Escape)
    assert not v._fullscreen and not v._header.isHidden()
    v.teardown()


def test_bare_view_still_works():
    v = VisualizerView()
    render(v)
    v.teardown()


def test_choice_providers_cover_every_tile_layout_and_classic():
    from tide.ui import settings_schema
    kinds = [k for k, _l in settings_schema.visualizer_tile_choices()]
    # core's tiles, plus the store's with no plugin on (a saved pick of one
    # has to survive the settings dialog)
    assert set(kinds) == set(vis_play.KIND_LABELS) | set(vis_play.MOVED_TILES)
    assert [k for k, _l in settings_schema.visualizer_layout_choices()] == list(LAYOUTS)
    classic = [k for k, _l in settings_schema.visualizer_classic_choices()]
    assert classic[0] == "theme" and set(renderer_slugs()) <= set(classic)

def test_a_store_tile_without_its_plugin_is_a_placeholder():
    v, s, _amb = _view(tiles=("spectrum", "halo", "lyrics", "river"))
    kinds = [t.kind for t in v.tiles()]
    assert kinds == ["spectrum", "halo", "lyrics", "river"]      # the picks stay
    assert isinstance(v.tiles()[1], MissingTile)
    assert "more visualizer tiles" in v.tiles()[1].message
    render(v)                                     # it paints its line
    v.teardown()


def test_a_plugin_tile_comes_and_goes():
    from tide.ui.vis_play import PluginTile, register_tile, unregister_tile
    from tide.ui.vis_deck import CanvasTile
    v, s, _amb = _view(tiles=("spectrum", "halo", "lyrics", "scope"))
    made = []

    def factory(engine):
        made.append(CanvasTile(engine))
        return made[-1]
    register_tile(PluginTile("halo", "art halo", factory, (), {}, lambda k, val: None))
    try:
        assert v.tiles()[1] in made and v.tiles()[1].plugin_settings == {}
        assert ("halo", "art halo") in vis_play.tile_kinds()
    finally:
        unregister_tile("halo")
    assert isinstance(v.tiles()[1], MissingTile)
    v.teardown()


# ---------- ambient: the watch-only beat clock ----------

def test_watching_holds_the_clock_and_map_without_pulsing_the_backdrop(h):
    h.player.change_state(PlayState.PLAYING)
    h.controller.set_watching("visualizer", True)
    h.position(0.0, 0.0)
    h.beats.updated.emit(KEY, flat_beat_map(0.3))
    assert h.beats.requests                     # the map was asked for
    assert not h.controller._render_timer.isActive()
    h.position(0.25, 0.25)
    h.now = 100.3
    clock = h.controller.beat_clock()
    assert clock is not None and clock.position == pytest.approx(0.3, abs=0.01)
    h.render(0.3)
    assert all(level == 0.0 for _t, level in h.samples)   # backdrop untouched
    h.controller.set_watching("visualizer", False)
    assert h.controller.beat_clock() is None    # nobody wants it: let go
    assert h.feed.adds == h.feed.removes == 1


def test_pulse_still_drives_the_backdrop_while_watched(h):
    h.controller.set_watching("visualizer", True)
    h.start()
    assert h.controller._render_timer.isActive()
    h.beats.updated.emit(KEY, flat_beat_map(0.3))
    h.render(0.05)
    h.render(0.30)
    assert h.samples[-1][1] == pytest.approx(1.0, abs=0.02)
    h.controller.set_pulse_enabled(False)       # watch-only again
    assert not h.controller._render_timer.isActive()
    assert h.samples[-1][1] == 0.0
    assert h.controller.beat_clock() is not None


def test_paused_clock_keeps_the_map_without_a_position(h):
    h.controller.set_watching("visualizer", True)
    h.start(enabled=False)
    h.beats.updated.emit(KEY, flat_beat_map(0.3))
    h.player.change_state(PlayState.PAUSED)
    clock = h.controller.beat_clock()
    assert clock is not None and clock.position is None



def test_live_trace_keeps_buckets_not_every_pulse():
    live = LiveTrace(seconds=4.0)
    for i in range(43 * 10):                    # ten seconds of the feed
        live.push(100.0 + i / 43.0, 0.5 + 0.5 * ((i % 20) == 0))
    assert len(live.samples) <= 4.0 / LiveTrace.BUCKET + 2
    assert max(v for _t, v in live.samples) == 1.0   # peaks survive the bucketing
    assert live.decay(live.last_at) == pytest.approx(live.level)


# ---------- capture that stops on a song change ----------

class _Proc:
    """a parec that delivers ``chunks`` of audio, then exits."""

    def __init__(self, feed, chunks, last=False):
        self._feed = feed
        self._reads = [np.zeros(1024, dtype=np.float32).tobytes()] * chunks
        self._last = last
        self.terminated = False
        self.stdout = self

    def read(self, _size):
        if self._reads:
            return self._reads.pop()
        if self._last:
            self._feed._stop.set()
        return b""

    def poll(self):
        return 0                                # exited

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0


class _Stop:
    def __init__(self):
        self._set = False

    def is_set(self):
        return self._set

    def set(self):
        self._set = True

    def clear(self):
        self._set = False

    def wait(self, _timeout=None):
        return self._set


def test_song_changes_never_use_up_the_respawns(monkeypatch):
    from tide import audio_capture
    feed = audio_capture.AudioVisualizerFeed()
    feed._stop = _Stop()
    feed._stream_idx = 7
    feed._running = True
    # ten song changes in a row that each land back on stream 7: every
    # parec delivered audio, then lost its stream
    procs = [_Proc(feed, 2, last=(i == 9)) for i in range(10)]
    feed._proc = _Proc(feed, 2)
    spawned = []
    monkeypatch.setattr(feed, "_resolve_target", lambda: (None, 7))
    monkeypatch.setattr(feed, "_spawn_parec",
                        lambda _m, _i=None: spawned.append(1) or procs[len(spawned) - 1])
    errors = []
    feed.error.connect(errors.append)
    feed._process_loop()
    assert len(spawned) == 10
    assert feed._running and not errors


def test_a_parec_that_never_delivers_still_gives_up(monkeypatch):
    from tide import audio_capture
    feed = audio_capture.AudioVisualizerFeed()
    feed._stop = _Stop()
    feed._stream_idx = 7
    feed._running = True
    feed._proc = _Proc(feed, 0)
    monkeypatch.setattr(feed, "_resolve_target", lambda: (None, 7))
    monkeypatch.setattr(feed, "_spawn_parec", lambda _m, _i=None: _Proc(feed, 0))
    errors = []
    feed.error.connect(errors.append)
    feed._process_loop()
    assert not feed._running and errors


def test_retarget_kicks_a_capture_left_on_an_old_stream(monkeypatch):
    from tide import audio_capture
    feed = audio_capture.AudioVisualizerFeed()
    feed._running = True
    feed._stream_idx = 7
    feed._proc = _Proc(feed, 0)
    monkeypatch.setattr(audio_capture, "_own_sink_input", lambda: 7)
    assert not feed.retarget() and not feed._proc.terminated
    monkeypatch.setattr(audio_capture, "_own_sink_input", lambda: 9)
    assert feed.retarget() and feed._proc.terminated
    feed._explicit_source = "desktop.monitor"   # a picked source is left alone
    assert not feed.retarget()
    feed._running = False


def test_watchdog_restarts_a_stopped_feed_and_retargets_a_quiet_one(monkeypatch):
    from tide.ui import visualizer as vis
    v = VisualizerView()
    v.show()
    feed = v._feed
    adds, kicks = [], []
    monkeypatch.setattr(feed, "add_consumer", lambda name, source=None: adds.append(name) or True)
    monkeypatch.setattr(feed, "retarget", lambda: kicks.append(1) or True)
    clock = [1000.0]
    monkeypatch.setattr(vis.time, "monotonic", lambda: clock[0])
    v._on_state(PlayState.PLAYING)
    v._capturing = True
    monkeypatch.setattr(type(feed), "running", property(lambda self: False))
    v._last_retry = -1e9
    v._check_capture()
    assert adds == ["visualizer"]               # stopped under us: restarted
    monkeypatch.setattr(type(feed), "running", property(lambda self: True))
    v._on_bands(np.zeros(32))
    clock[0] += 1.0
    v._check_capture()
    assert not kicks                            # bands flowing: leave it
    clock[0] += 5.0
    v._check_capture()
    assert kicks == [1]                         # quiet while playing: retarget
    v._on_state(PlayState.PAUSED)
    clock[0] += 10.0
    v._check_capture()
    assert kicks == [1]                         # paused is quiet on purpose
    v.teardown()


# ---------- the spectrum that went flat for good ----------

def test_one_nan_sample_no_longer_poisons_the_spectrum():
    from tide.audio_capture import CHUNK, _compute_bands
    rng = np.random.default_rng(0)

    def loud():
        return (0.5 * np.sin(np.linspace(0, 200, CHUNK))
                + 0.1 * rng.normal(size=CHUNK)).astype(np.float32)

    prev = None
    for _ in range(3):
        prev = _compute_bands(loud(), prev)
    bad = loud()
    bad[100] = np.nan
    bad[200] = np.inf
    prev = _compute_bands(bad, prev)
    assert np.isfinite(prev).all()
    for _ in range(20):
        prev = _compute_bands(loud(), prev)
    assert np.isfinite(prev).all() and prev.max() > 0.2      # still moving
    # a history poisoned before this fix recovers too
    assert np.isfinite(_compute_bands(loud(), np.full(prev.shape, np.nan))).all()


def test_the_feed_keeps_emitting_live_bands_after_a_glitched_chunk(monkeypatch):
    from tide import audio_capture
    from tide.audio_capture import CHUNK
    feed = audio_capture.AudioVisualizerFeed()
    feed._stop = _Stop()
    feed._stream_idx = 7
    feed._spectrum = True
    tone = (0.5 * np.sin(np.linspace(0, 200, CHUNK))).astype(np.float32)
    bad = tone.copy()
    bad[5] = np.nan

    class P(_Proc):
        def __init__(self):
            super().__init__(feed, 0, last=True)
            self._reads = [tone.tobytes()] * 5 + [bad.tobytes()] + [tone.tobytes()] * 3
            self._reads.reverse()

    feed._proc = P()
    bands, waves = [], []
    feed.bands_updated.connect(bands.append)
    feed.waveform_updated.connect(waves.append)
    feed._process_loop()
    assert len(bands) == 9
    assert all(np.isfinite(b).all() for b in bands)
    assert bands[-1].max() > 0.2
    assert all(np.isfinite(w).all() for w in waves)


def test_stream_mode_follows_tide_onto_a_new_stream(monkeypatch):
    """a song change moved tide to stream 9 while 7 lingered feeding
    silence: parec stayed on 7 with no EOF, so nothing ever respawned."""
    from tide import audio_capture
    feed = audio_capture.AudioVisualizerFeed()
    feed._stop = _Stop()
    feed._stream_idx = 7
    feed._running = True
    feed._next_upgrade = 1.0
    clock = [0.0]
    monkeypatch.setattr(audio_capture.time, "monotonic", lambda: clock[0])
    own = [7]
    monkeypatch.setattr(audio_capture, "_own_sink_input", lambda: own[0])

    class Silent(_Proc):
        def read(self, _size):
            clock[0] += 0.5
            if clock[0] > 2.0:
                own[0] = 9                       # the song changed
            return np.zeros(1024, dtype=np.float32).tobytes()

    first = Silent(feed, 0)
    feed._proc = first
    spawned = []

    def spawn(_monitor, idx=None):
        spawned.append(idx)
        return _Proc(feed, 1, last=True)

    monkeypatch.setattr(feed, "_resolve_target", lambda: (None, own[0]))
    monkeypatch.setattr(feed, "_spawn_parec", spawn)
    monkeypatch.setattr(feed._stop, "wait", lambda _t=None: False)
    feed._process_loop()
    assert first.terminated
    assert spawned == [9] and feed._stream_idx == 9


def test_one_tile_throwing_cannot_freeze_the_others(monkeypatch):
    v, _s, _amb = _view(tiles=("spectrum", "mirror", "deck", "scope"))
    v.show()
    e = v.engine
    _feed(e)
    spectrum, life = v.tiles()[0], v.tiles()[1]     # "life": the one that throws
    monkeypatch.setattr(life, "on_frame", lambda *_a: 1 / 0)
    monkeypatch.setattr(life, "paint_graphics", lambda *_a: 1 / 0)
    seen = []
    orig = spectrum.on_frame
    monkeypatch.setattr(spectrum, "on_frame", lambda *a: (seen.append(1), orig(*a)))
    for _ in range(3):
        e.tick()
        render(v)                                # life's paint throws every frame
    assert len(seen) == 3
    assert not life.buffer().isNull()
    render(v)                                    # and its painter wasn't left open
    v.teardown()


def test_engine_drops_nan_bands_and_positions():
    clock = [BeatClock(song(), float("nan"), 1.0)]
    e = _engine(clock)
    e.update_bands(np.full(32, np.nan))
    assert np.isfinite(e.bands).all()
    e.update_waveform(np.array([np.nan, np.inf, 0.5], dtype=np.float32))
    assert np.isfinite(e.wave).all()
    e.set_playing(True)
    e.tick()
    assert e.position is None or np.isfinite(e.position)


# ---------- editing a tile ----------

def test_every_tile_has_a_hover_edit_button_that_opens_its_menu():
    from PySide6.QtCore import QEvent
    v, _s, _amb = _view(tiles=("spectrum", "halo", "lyrics", "tapper"))
    v.show()
    grid = v._grid
    asked = []
    grid.menu_requested.connect(lambda slot, pos: asked.append(slot))
    for slot, tile in enumerate(v.tiles()):
        btn = tile._edit_btn
        plate = tile._edit_plate
        assert btn.parentWidget() is plate and btn.text() != ""
        assert plate.isHidden() and not btn.isVisible()
        QApplication.sendEvent(tile, QEvent(QEvent.Type.Enter))
        assert btn.isVisible()
        assert tile._edit_plate.geometry().right() <= tile.width()
        btn.click()
        QApplication.sendEvent(tile, QEvent(QEvent.Type.Leave))
        assert plate.isHidden() and not btn.isVisible()
    assert asked == [0, 1, 2, 3]
    v.teardown()


def test_a_listener_gets_the_pulse_without_moving_the_backdrop(h):
    """plugins (the vr ring) listen through add_listener: they get every
    pulse while the backdrop pulse is off, and the backdrop stays still;
    switching the backdrop pulse off mid-swell puts it at rest."""
    class Listener:
        def __init__(self):
            self.levels = []

        def set_pulse(self, level):
            self.levels.append(level)

    ring = Listener()
    h.controller.add_listener(ring)
    h.start()
    h.beats.updated.emit(KEY, flat_beat_map(0.3))
    h.render(0.05)
    h.render(0.30)
    assert h.samples[-1][1] == pytest.approx(1.0, abs=0.02)
    assert ring.levels[-1] == pytest.approx(1.0, abs=0.02)
    h.controller.set_pulse_enabled(False)       # backdrop off, the ring still listens
    assert h.samples[-1][1] == 0.0
    before = len(h.samples)
    h.render(0.55)
    assert len(h.samples) == before             # the backdrop gets nothing more
    assert len(ring.levels) > 2
    h.controller.remove_listener(ring)
    assert ring.levels[-1] == 0.0
