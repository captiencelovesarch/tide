"""Sparse backend reports must not hide hits between clock updates."""
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QObject, QThread, Qt, Signal
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QApplication
from shiboken6 import isValid

from tide.audio_capture import PulseFrame
from tide.player import PlayState
from tide.pulse_map import PulseEvent, PulseMap, PulseScheduler
from tide.ui import ambient


_APP = QApplication.instance() or QApplication([])


class Player(QObject):
    state_changed = Signal(object)
    position_changed = Signal(float)
    speed_changed = Signal(float)

    def __init__(self):
        super().__init__()
        self.state = PlayState.IDLE
        self.speed = 1.0
        self.supports_speed = True

    def active_supports_speed(self):
        return self.supports_speed

    def change_state(self, state):
        self.state = state
        self.state_changed.emit(state)


class Feed(QObject):
    pulse_updated = Signal(float)
    pulse_frame = Signal(object)

    def __init__(self):
        super().__init__()
        self.adds = 0
        self.removes = 0

    def add_consumer(self, *_args, **_kwargs):
        self.adds += 1

    def remove_consumer(self, *_args):
        self.removes += 1


class Store(QObject):
    loaded = Signal(int, str, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.pulse_map = None
        self.loads = 0
        self.saves = 0

    def load(self, generation, key):
        self.loads += 1
        self.loaded.emit(generation, key, self.pulse_map)

    def save_recording(self, *_args):
        self.saves += 1

    def close(self):
        pass


def flat_map(value=0.8, end=10.0):
    return PulseMap((PulseEvent(0.0, value), PulseEvent(end, value)), ((0.0, end),))


class Harness:
    def __init__(self, monkeypatch):
        self.now = 100.0
        self.player = Player()
        self.feed = Feed()
        self.samples = []
        self.target = SimpleNamespace(set_pulse=lambda value: self.samples.append((self.now - 100.0, value)))
        monkeypatch.setattr(ambient.audio_capture, "feed", lambda: self.feed)
        monkeypatch.setattr(ambient, "PulseMapStore", Store)
        monkeypatch.setattr(ambient, "time", SimpleNamespace(monotonic=lambda: self.now))
        monkeypatch.setattr(ambient.settings_module, "load", lambda: SimpleNamespace(audio_device=""))
        self.controller = ambient.AmbientController(
            self.player, self.target,
            track_provider=lambda: SimpleNamespace(source="spotify", video_id="clock-song"))
        self.store = self.controller._store
        self.store.pulse_map = flat_map()

    def start(self, enabled=True):
        self.controller.set_pulse_enabled(enabled)
        self.player.change_state(PlayState.PLAYING)
        self.position(0.0, 0.0)

    def position(self, wall, media):
        self.now = 100.0 + wall
        self.player.position_changed.emit(media)

    def frame(self, wall, level=0.8, *, captured=None, latency=0.0,
              reset=False, scoped=True, onset=None, energy=None, kind="kick"):
        self.now = 100.0 + wall
        self.feed.pulse_updated.emit(level)
        self.feed.pulse_frame.emit(PulseFrame(
            100.0 + (wall if captured is None else captured), level,
            kind, latency, reset, scoped, onset=onset, energy=energy))

    def render(self, wall):
        self.now = 100.0 + wall
        self.controller._on_render_tick()

    def drive(self, end=0.8, speed=1.0):
        # 10 ms simulation grid aligns 250 ms backend and 20 ms render ticks.
        for step in range(1, round(end * 100) + 1):
            wall = step / 100
            if step % 25 == 0:
                self.position(wall, wall * speed)
            if step % 2 == 0:
                value = self.store.pulse_map.value_at(wall * speed) or 0.0
                self.frame(wall, value)
                self.render(wall)


@pytest.fixture
def h(monkeypatch):
    harness = Harness(monkeypatch)
    yield harness
    harness.controller.shutdown()


@pytest.mark.parametrize("phase", [i / 100 for i in range(25)])
def test_250ms_backend_clock_catches_every_beat_phase_on_render_ticks(h, phase):
    hits = [1.0 + phase, 1.5 + phase, 2.0 + phase]
    events = [PulseEvent(0.0, 0.0)]
    for hit in hits:
        events.extend((PulseEvent(hit - 0.0001, 0.0), PulseEvent(hit, 1.0),
                       PulseEvent(hit + 0.1, 0.0)))
    events.append(PulseEvent(3.0, 0.0))
    h.store.pulse_map = PulseMap(tuple(events), ((0.0, 3.0),))
    h.start()
    h.drive(2.8)
    assert h.controller._scheduled
    assert not h.controller._scheduler.disagreed
    for hit in hits:
        caught = [(wall, level) for wall, level in h.samples
                  if hit - 1e-8 <= wall <= hit + 0.021 and level >= 0.799]
        assert caught, (phase, hit)
        assert caught[0][0] - hit <= 0.020001


def test_render_timer_is_gui_owned_precise_and_stops_without_consumers(h):
    h.start(enabled=False)
    timer = h.controller._render_timer
    assert timer.parent() is h.controller
    assert timer.thread() is h.controller.thread()
    assert timer.timerType() == Qt.TimerType.PreciseTimer
    assert timer.interval() == 20
    assert not timer.isActive()
    h.drive()
    assert (h.feed.adds, h.store.loads, h.store.saves) == (0, 0, 0)
    h.controller.set_pulse_enabled(True)
    assert timer.isActive()
    assert (h.feed.adds, h.store.loads) == (1, 1)
    h.controller.remove_target(h.target)
    assert not timer.isActive()
    assert h.feed.removes == 1
    counts = (h.feed.adds, h.store.loads, h.store.saves)
    h.controller.set_mini_active(True)
    h.drive()
    assert (h.feed.adds, h.store.loads, h.store.saves) == counts
    assert not timer.isActive()


def test_disabling_last_consumer_stops_rendering_and_cache_work(h):
    h.start()
    h.drive()
    h.controller.set_pulse_enabled(False)
    assert not h.controller._render_timer.isActive()
    assert not h.controller._holding
    samples = len(h.samples)
    counts = (h.feed.adds, h.store.loads, h.store.saves)
    h.position(1.0, 1.0)
    h.frame(1.0)
    h.render(1.0)
    assert len(h.samples) == samples
    assert (h.feed.adds, h.store.loads, h.store.saves) == counts


def test_every_render_starts_from_latest_backend_anchor(h):
    h.store.pulse_map = PulseMap((PulseEvent(0.0, 0.0), PulseEvent(10.0, 1.0)), ((0.0, 10.0),))
    h.start()
    h.drive()
    h.position(1.0, 1.02)
    h.frame(1.0, 0.102)
    anchors = tuple(h.controller._anchors)
    h.render(1.02)
    assert h.samples[-1][1] == pytest.approx(0.104)
    h.render(1.04)
    assert h.samples[-1][1] == pytest.approx(0.106)
    assert tuple(h.controller._anchors) == anchors
    assert anchors[-1] == (101.0, 1.02)


def test_learning_waits_for_real_clock_bracket_despite_render_ticks(h):
    h.start()
    recorded = []
    h.controller._recorder = SimpleNamespace(
        record=lambda position, *_args, **_kwargs: recorded.append(position),
        gap=lambda: None, segments=lambda: ())
    h.position(0.25, 0.27)
    h.frame(0.40, captured=0.40, latency=0.08)
    h.render(0.42)
    h.render(0.46)
    assert recorded == []
    h.position(0.50, 0.55)
    assert recorded == pytest.approx([0.27 + 0.15 * (0.28 / 0.25) - 0.08])


def test_clock_stall_falls_back_and_expires_without_new_signals(h):
    h.start()
    h.drive()
    assert h.controller._scheduled
    h.render(1.11)
    assert not h.controller._scheduled
    h.render(1.25)
    assert h.samples[-1][1] == pytest.approx(0.0)


def test_fresh_capture_cannot_extend_stalled_media_clock(h):
    h.start()
    h.drive()
    for wall in (0.90, 1.00, 1.10, 1.20, 1.30, 1.40):
        h.frame(wall, 0.2)
        h.render(wall)
    assert not h.controller._scheduled
    assert h.samples[-1][1] == pytest.approx(0.2)
    assert h.controller._anchors[-1] == (100.75, 0.75)


def test_fresh_clock_cannot_extend_stalled_capture(h):
    h.start()
    h.drive()
    for wall in (1.00, 1.25, 1.40):
        h.position(wall, wall)
        h.render(wall)
    assert not h.controller._scheduled
    assert h.samples[-1][1] == pytest.approx(0.0)


@pytest.mark.parametrize("media", [0.75, 0.1, 5.0])
def test_duplicate_backward_and_forward_seek_stop_extrapolation(h, media):
    h.start()
    h.drive()
    h.position(1.0, media)
    assert not h.controller._scheduled
    assert len(h.controller._anchors) == 1
    h.frame(1.20)
    h.render(1.20)
    assert not h.controller._scheduled
    h.position(1.25, media + 0.25)
    assert h.controller._scheduled


def test_repeated_duplicate_positions_never_resume_schedule(h):
    h.start()
    h.drive()
    for wall in (1.0, 1.25, 1.5):
        h.position(wall, 0.75)
        h.frame(wall + 0.16)
        h.render(wall + 0.16)
        assert not h.controller._scheduled
    assert len(h.controller._anchors) == 1


def test_seek_outside_coverage_stays_live_after_clock_recovers(h):
    h.store.pulse_map = flat_map(end=2.0)
    h.start()
    h.drive()
    h.position(1.0, 5.0)
    h.frame(1.20, 0.2)
    h.position(1.25, 5.25)
    h.render(1.26)
    assert not h.controller._scheduled
    assert h.samples[-1][1] == pytest.approx(0.2)


@pytest.mark.parametrize(("speed", "supported", "effective"), [
    (0.5, True, 0.5), (1.0, True, 1.0), (2.0, True, 2.0), (2.0, False, 1.0)])
def test_render_extrapolation_passes_effective_playback_speed(h, speed, supported, effective):
    h.player.speed = speed
    h.player.supports_speed = supported
    h.start()
    calls = []
    scheduler = PulseScheduler(h.store.pulse_map)
    scheduler.value_at = lambda position, rate=1.0: calls.append((position, rate)) or 0.8
    h.controller._scheduler = scheduler
    h.drive(speed=effective)
    h.render(0.82)
    assert calls[-1] == pytest.approx((0.82 * effective, effective))


def test_speed_change_discards_old_anchor_before_rendering(h):
    h.start()
    h.drive()
    h.now = 100.9
    h.player.speed = 2.0
    h.player.speed_changed.emit(2.0)
    h.render(0.92)
    assert not h.controller._scheduled
    assert not h.controller._anchors
    h.position(1.0, 1.0)
    h.frame(1.20)
    h.position(1.25, 1.50)
    h.render(1.26)
    assert h.controller._scheduled
    assert h.controller._anchors[-1] == (101.25, 1.50)


def test_pause_stops_timer_and_resume_waits_for_fresh_clock(h):
    h.start()
    h.drive()
    h.player.change_state(PlayState.PAUSED)
    assert not h.controller._render_timer.isActive()
    assert h.samples[-1][1] == 0.0
    samples = len(h.samples)
    h.render(1.0)
    assert len(h.samples) == samples
    h.now = 102.0
    h.player.change_state(PlayState.PLAYING)
    assert h.controller._render_timer.isActive()
    h.position(2.0, 0.8)
    h.frame(2.2)
    h.render(2.2)
    assert not h.controller._scheduled
    h.position(2.25, 1.05)
    assert h.controller._scheduled
    h.player.change_state(PlayState.IDLE)
    assert not h.controller._render_timer.isActive()


@pytest.mark.parametrize(("reset", "scoped"), [(True, True), (False, False)])
def test_reset_or_scope_loss_cannot_replay_from_old_anchor(h, reset, scoped):
    h.start()
    h.drive()
    h.frame(0.82, 0.2, reset=reset, scoped=scoped)
    h.render(1.0)
    assert not h.controller._scheduled
    assert not h.controller._anchors
    assert not h.controller._scoped
    h.frame(1.02, 0.2)
    h.render(1.02)
    assert not h.controller._scheduled
    h.position(1.05, 1.05)
    h.position(1.30, 1.30)
    assert h.controller._scheduled


def test_qt_timer_runs_on_gui_thread_and_dies_with_controller(monkeypatch):
    h = Harness(monkeypatch)
    timer = h.controller._render_timer
    threads = []
    set_pulse = h.target.set_pulse

    def record_thread(value):
        threads.append(QThread.currentThread())
        set_pulse(value)

    h.target.set_pulse = record_thread
    try:
        h.start()
        h.drive()
        threads.clear()
        before = len(h.samples)
        spy = QSignalSpy(timer.timeout)
        assert spy.wait(250)
        assert len(h.samples) > before
        assert threads and all(thread == _APP.thread() for thread in threads)
    finally:
        h.controller.shutdown()
        h.controller.deleteLater()
        QCoreApplication.sendPostedEvents(h.controller, QEvent.Type.DeferredDelete)
    assert not isValid(h.controller)
    assert not isValid(timer)


def test_detailed_frames_learn_and_replay_off_grid_hit_with_sparse_clock(h):
    h.store.pulse_map = None
    h.start()

    def drive(start):
        for step in range(1, 201):
            media = step / 100
            wall = start + media
            if step % 25 == 0:
                h.position(wall, media)
            onset = 0.9 if step == 113 else 0.0
            h.frame(wall, 0.2, onset=onset,
                    energy=0.36 if onset else 0.02,
                    kind="kick" if onset else "sustain")
            if step % 2 == 0:
                h.render(wall)

    drive(0.0)
    learned = h.controller._recorder.snapshot()
    assert learned is not None and len(learned.hits) == 1
    hit = learned.hits[0]
    assert hit.media_time == pytest.approx(1.13)
    assert hit.strength == pytest.approx(0.9)
    assert hit.energy == pytest.approx(0.36)
    assert hit.kind == "kick"
    assert learned.reference_at(1.13) == pytest.approx(0.2)

    h.player.change_state(PlayState.IDLE)
    h.store.pulse_map = learned
    h.now = 103.0
    h.player.change_state(PlayState.PLAYING)
    h.position(3.0, 0.0)
    h.samples.clear()
    drive(3.0)
    assert h.controller._scheduled
    assert not h.controller._scheduler.disagreed
    assert all(level == 0.0 for wall, level in h.samples if 4.0 <= wall <= 4.12)
    caught = [(wall, level) for wall, level in h.samples
              if 4.13 <= wall <= 4.151 and level >= 0.899]
    assert caught
    assert caught[0][0] - 4.13 <= 0.020001
