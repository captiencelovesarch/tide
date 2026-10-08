"""audible attacks survive imperfect maps and arbitrary paint phases."""
from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget

from tide.player import PlayState
from tide.pulse_map import PulseEvent, PulseHit, PulseMap, PulseScheduler
from tide.ui import central_bg
from test_pulse_clock import h  # shared player/capture clock harness


def _start_map(h, hits=()):
    h.store.pulse_map = PulseMap(
        (PulseEvent(0.0, 0.0), PulseEvent(10.0, 0.0)), ((0.0, 10.0),), hits)
    h.start()
    h.drive(speed=h.player.speed)
    assert h.controller._scheduled


@pytest.mark.parametrize("speed", [0.5, 1.0, 2.0])
def test_uncached_attack_is_visible_before_next_clock_report(h, speed):
    h.player.speed = speed
    _start_map(h)
    h.frame(0.83, 0.9, onset=0.9, latency=0.025)
    assert h.samples[-1] == pytest.approx((0.83, 0.9))
    assert h.controller._anchors[-1][0] == 100.75
    assert h.controller._scheduled
    h.frame(0.84, 0.0, onset=0.0, kind="sustain")
    h.render(0.846)
    assert 0.7 < h.samples[-1][1] < 0.9


@pytest.mark.parametrize("speed", [0.5, 1.0, 2.0])
def test_delayed_capture_does_not_retrigger_a_cached_hit(h, speed):
    h.player.speed = speed
    _start_map(h, (PulseHit(0.81 * speed, 0.9),))
    h.position(0.80, 0.80 * speed)
    h.render(0.81)
    h.frame(0.835, 0.9, onset=0.9, latency=0.025)
    assert h.controller._rescue_level == 0.0


@pytest.mark.parametrize("kwargs", [
    {"onset": 0.0, "kind": "sustain"},
    {"onset": 0.9, "scoped": False},
    {"onset": 0.9, "captured": 0.60},
])
def test_sustain_unscoped_and_queued_old_audio_do_not_rescue(h, kwargs):
    _start_map(h)
    h.frame(0.83, 0.9, **kwargs)
    assert h.controller._rescue_level == 0.0


def test_seek_and_pause_clear_the_rescued_tail(h):
    _start_map(h)
    h.frame(0.83, 0.9, onset=0.9)
    h.position(0.84, 4.0)
    assert h.controller._rescue_level == 0.0
    h.player.change_state(PlayState.PAUSED)
    assert h.samples[-1][1] == 0.0
    assert not h.controller._render_timer.isActive()


@pytest.mark.parametrize("speed", [0.5, 1.0, 2.0])
@pytest.mark.parametrize("skew", [-0.035, 0.035])
def test_consistent_capture_skew_aligns_replay_without_rewriting_map(speed, skew):
    hits = tuple(PulseHit((1.0 + i * 0.5) * speed, 0.9) for i in range(14))
    pulse_map = PulseMap((PulseEvent(0.0, 0.1), PulseEvent(20.0, 0.1)),
                         ((0.0, 20.0),), hits)
    scheduler = PulseScheduler(pulse_map)
    for step in range(1, 580):
        wall = step * 0.01
        onset = 0.9 if any(abs(wall - (hit.media_time / speed + skew)) < 0.006
                           for hit in hits) else 0.0
        scheduler.observe(wall * speed, 0.1, speed, onset=onset)
    assert not scheduler.disagreed
    assert abs(scheduler._phase_offset / speed - skew) < 0.011
    future = hits[11].media_time + scheduler._phase_offset
    assert scheduler.value_at(future - 0.001, speed) < 0.2
    assert scheduler.value_at(future + 0.001, speed) > 0.89
    assert scheduler.pulse_map is pulse_map
    scheduler.gap()
    assert scheduler._phase_offset == 0.0


def test_inconsistent_peak_timing_cannot_drag_the_replay_clock():
    hits = tuple(PulseHit(1.0 + i * 0.5, 0.9) for i in range(14))
    pulse_map = PulseMap((PulseEvent(0.0, 0.1), PulseEvent(10.0, 0.1)),
                         ((0.0, 10.0),), hits)
    scheduler = PulseScheduler(pulse_map)
    for step in range(1, 650):
        wall = step * 0.01
        onset = 0.9 if any(abs(wall - hit.media_time - (0.04 if i % 2 else -0.04)) < 0.001
                           for i, hit in enumerate(hits)) else 0.0
        scheduler.observe(wall, 0.1, onset=onset)
    assert scheduler._phase_offset == 0.0


@pytest.fixture
def backdrop(monkeypatch):
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr(central_bg, "time", SimpleNamespace(monotonic=lambda: clock.now))
    widget = central_bg.CentralBg(QWidget())
    widget.set_enabled(True)
    widget.set_motion("full")
    widget.show()
    yield widget, clock
    widget.hide()
    widget.deleteLater()


@pytest.mark.parametrize("phase", range(42))
def test_pulse_reaches_full_strength_within_one_display_frame(backdrop, phase):
    widget, clock = backdrop
    clock.now += phase / 1000
    widget.set_pulse(0.9)
    assert widget._anim.interval() == 16
    assert widget._anim.timerType() == Qt.TimerType.PreciseTimer
    if widget._pulse_shown < 0.9:
        clock.now += 0.016
        widget._tick()
    assert widget._pulse_shown == pytest.approx(0.9)
    assert clock.now - 100.0 - phase / 1000 <= 0.016001


def test_short_attack_between_paints_is_not_overwritten(backdrop):
    widget, clock = backdrop
    clock.now += 0.001
    widget.set_pulse(0.9)
    clock.now += 0.001
    widget.set_pulse(0.0)
    clock.now = 100.016
    widget._tick()
    assert widget._pulse_shown == pytest.approx(0.9)
    clock.now += 0.016
    widget._tick()
    assert 0.0 < widget._pulse_shown < 0.9


@pytest.mark.parametrize("step", [0.008, 0.016, 0.040])
def test_release_and_palette_fade_follow_elapsed_time(backdrop, step):
    widget, clock = backdrop
    widget._pulse_shown = 1.0
    widget._tone_blend = 0.0
    for i in range(round(0.08 / step)):
        clock.now = 100.0 + (i + 1) * step
        widget._tick()
    assert widget._pulse_shown == pytest.approx(0.06948345, abs=0.00001)
    assert widget._tone_blend == pytest.approx(0.080 / 1.4)


def test_pulse_returns_to_idle_rate_and_hidden_backdrop_stops(backdrop):
    widget, clock = backdrop
    widget.set_pulse(1.0)
    clock.now += 0.016
    widget._tick()
    widget.set_pulse(0.0)
    for _ in range(30):
        clock.now += 0.016
        widget._tick()
    # the gradient looks idle slower than the scene styles: their drift is
    # sub-pixel per frame, and each frame repaints the whole window
    assert widget._anim.interval() == central_bg._GRADIENT_IDLE_MS
    widget.set_style("horizon")
    clock.now += 0.016
    widget._tick()
    assert widget._anim.interval() == central_bg._ANIM_INTERVAL_MS == 42
    widget.set_motion("off")
    assert not widget._anim.isActive()
    widget.set_pulse(1.0)
    widget.hide()
    assert not widget._anim.isActive()


def test_hidden_backdrop_does_not_save_old_attacks_for_when_it_reopens(backdrop):
    widget, clock = backdrop
    widget.hide()
    widget.set_pulse(0.9)
    clock.now += 0.2
    widget.set_pulse(0.0)
    widget.show()
    clock.now += 0.016
    widget._tick()
    assert widget._pulse_shown == 0.0
