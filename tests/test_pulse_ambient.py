"""Playback transitions must not teach one track another track's pulse."""
import math
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from tide.audio_capture import PulseFrame
from tide.player import PlayState
from tide.pulse_map import PulseEvent, PulseMap, PulseScheduler, map_from_segments, merge_maps, track_key
from tide.ui import ambient


_APP = QApplication.instance() or QApplication([])


class FakePlayer(QObject):
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


class FakeFeed(QObject):
    pulse_updated = Signal(float)
    pulse_frame = Signal(object)

    def __init__(self):
        super().__init__()
        self.added = []
        self.removed = []

    def add_consumer(self, name, source=None):
        self.added.append((name, source))
        return True

    def remove_consumer(self, name):
        self.removed.append(name)


class FakeStore(QObject):
    loaded = Signal(int, str, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.maps = {}
        self.loads = []
        self.saves = []
        self.closed = False
        self.deliver = True

    def load(self, generation, key):
        self.loads.append((generation, key))
        if self.deliver:
            self.loaded.emit(generation, key, self.maps.get(key))

    def save(self, key, pulse_map):
        self.saves.append((key, pulse_map))
        self.maps[key] = merge_maps(self.maps.get(key), pulse_map)

    def save_recording(self, key, segments):
        pulse_map = map_from_segments(segments)
        if pulse_map is not None:
            self.save(key, pulse_map)

    def close(self):
        self.closed = True


class Target:
    def __init__(self):
        self.values = []

    def set_pulse(self, value):
        self.values.append(value)


def track(video_id, source="ytmusic"):
    return SimpleNamespace(video_id=video_id, source=source)


def flat_map(start=0.0, end=20.0, value=0.7):
    return PulseMap((PulseEvent(start, value), PulseEvent(end, value)), ((start, end),))


class Harness:
    def __init__(self, monkeypatch, initial_state=PlayState.IDLE):
        self.now = 100.0
        self.player = FakePlayer()
        self.player.state = initial_state
        self.feed = FakeFeed()
        self.target = Target()
        self.current = track("a")
        monkeypatch.setattr(ambient.audio_capture, "feed", lambda: self.feed)
        monkeypatch.setattr(ambient, "PulseMapStore", FakeStore)
        monkeypatch.setattr(ambient, "time", SimpleNamespace(monotonic=lambda: self.now))
        monkeypatch.setattr(ambient.settings_module, "load", lambda: SimpleNamespace(audio_device=""))
        self.controller = ambient.AmbientController(
            self.player, self.target, track_provider=lambda: self.current)
        self.store = self.controller._store

    def time(self, wall):
        self.now = 100.0 + wall

    def start(self, *, enabled=True):
        self.controller.set_pulse_enabled(enabled)
        self.player.change_state(PlayState.LOADING)
        self.player.change_state(PlayState.PLAYING)
        self.position(0.0, 0.0)

    def position(self, wall, media):
        self.time(wall)
        self.player.position_changed.emit(media)

    def frame(self, wall, level=0.7, *, captured=None, latency=0.08,
              reset=False, scoped=True):
        self.time(wall)
        self.feed.pulse_updated.emit(level)
        self.feed.pulse_frame.emit(PulseFrame(
            100.0 + (wall if captured is None else captured), level,
            "kick", latency, reset, scoped))

    def tick(self, wall, media=None, level=0.7, **kwargs):
        self.position(wall, wall if media is None else media)
        self.frame(wall, level, **kwargs)

    def learn(self, speed=1.0):
        for i in range(4, 17):
            wall = i * 0.05
            self.tick(wall, wall * speed)


@pytest.fixture
def h(monkeypatch):
    harness = Harness(monkeypatch)
    yield harness
    harness.controller.shutdown()


def test_disabled_pulse_never_acquires_or_reads_maps(h):
    h.start(enabled=False)
    h.learn()
    assert h.feed.added == []
    assert h.store.loads == []
    assert h.store.saves == []
    assert not h.controller._holding


def test_no_targets_releases_capture_even_when_enabled(h):
    h.start()
    h.controller.remove_target(h.target)
    assert h.feed.removed == ["ambient"]
    assert not h.controller._holding
    h.controller.set_mini_active(True)
    assert len(h.feed.added) == 1


def test_mini_can_consume_without_main_pulse_setting(h):
    h.start(enabled=False)
    mini = Target()
    h.controller.add_target(mini)
    h.controller.set_mini_active(True)
    h.frame(0.2, 0.6)
    assert len(h.feed.added) == 1
    assert mini.values[-1] == pytest.approx(0.6)
    h.controller.set_mini_active(False)
    assert h.feed.removed == ["ambient"]


def test_selected_track_does_not_relabel_audio_before_stop(h):
    h.start()
    h.learn()
    a_key = track_key(h.current)
    h.current = track("b")
    h.tick(0.85)
    h.player.change_state(PlayState.IDLE)
    assert {key for key, _pulse_map in h.store.saves} == {a_key}
    assert track_key(h.current) not in h.store.maps
    h.player.change_state(PlayState.LOADING)
    h.player.change_state(PlayState.PLAYING)
    assert h.controller._key == track_key(h.current)


def test_failed_resolution_does_not_load_or_record_selected_track(h):
    h.start()
    h.learn()
    h.player.change_state(PlayState.IDLE)
    h.current = track("failed")
    h.player.change_state(PlayState.LOADING)
    h.frame(1.0)
    h.player.change_state(PlayState.IDLE)
    h.current = track("success")
    h.player.change_state(PlayState.LOADING)
    h.player.change_state(PlayState.PLAYING)
    assert [key for _generation, key in h.store.loads] == [track_key(track("a")), track_key(h.current)]
    assert track_key(track("failed")) not in h.store.maps


def test_repeat_same_track_flushes_before_reload(h):
    h.start()
    h.learn()
    key = track_key(h.current)
    h.player.change_state(PlayState.IDLE)
    h.player.change_state(PlayState.LOADING)
    h.player.change_state(PlayState.PLAYING)
    assert len(h.store.saves) == 1
    assert h.store.loads[-1][1] == key
    assert h.controller._scheduler.pulse_map is h.store.maps[key]


def test_track_provider_handles_instrumental_outside_queue(h):
    h.current = track("instrumental")
    h.start()
    h.learn()
    h.player.change_state(PlayState.IDLE)
    assert h.store.saves[0][0] == track_key(track("instrumental"))


def test_enabled_midplay_bootstraps_current_track(h):
    h.current = track("restored")
    h.player.change_state(PlayState.PLAYING)
    h.controller.set_pulse_enabled(True)
    assert h.store.loads[-1][1] == track_key(h.current)
    assert len(h.feed.added) == 1


def test_old_map_load_cannot_replace_new_track_map(h):
    h.store.deliver = False
    h.start()
    stale_generation, stale_key = h.store.loads[-1]
    h.player.change_state(PlayState.IDLE)
    h.current = track("b")
    h.player.change_state(PlayState.LOADING)
    h.player.change_state(PlayState.PLAYING)
    expected = flat_map(value=0.4)
    generation, key = h.store.loads[-1]
    h.store.loaded.emit(generation, key, expected)
    h.store.loaded.emit(stale_generation, stale_key, flat_map(value=1.0))
    assert h.controller._scheduler.pulse_map is expected


@pytest.mark.parametrize("speed", [0.5, 1.0, 2.0])
def test_frame_latency_is_normalized_at_recording_speed(h, speed):
    h.player.speed = speed
    h.start()
    h.learn(speed)
    learned = h.controller._recorder.snapshot()
    assert learned is not None
    assert learned.events[0].media_time == pytest.approx((0.2 - 0.08) * speed)
    assert learned.events[-1].media_time == pytest.approx((0.8 - 0.08) * speed)


def test_backend_without_speed_support_uses_actual_one_x(h):
    h.player.speed = 2.0
    h.player.supports_speed = False
    h.start()
    h.learn()
    learned = h.controller._recorder.snapshot()
    assert learned.events[0].media_time == pytest.approx(0.12)
    assert learned.events[-1].media_time == pytest.approx(0.72)


def test_pause_resume_rejects_buffered_frames_and_leaves_gap(h):
    h.start()
    h.tick(0.2)
    h.tick(0.25)
    h.time(0.3)
    h.player.change_state(PlayState.PAUSED)
    h.frame(0.4, captured=0.29)
    h.time(1.0)
    h.player.change_state(PlayState.PLAYING)
    h.position(1.0, 0.25)
    h.frame(1.16, captured=0.29)
    h.tick(1.2, 0.45)
    h.tick(1.25, 0.50)
    learned = h.controller._recorder.snapshot()
    assert len(learned.events) == 4
    assert learned.value_at(0.25) is None
    assert len(learned.coverage) == 2
    assert h.feed.removed == ["ambient"]


def test_position_and_capture_after_stop_cannot_record(h):
    h.start()
    h.learn()
    h.player.change_state(PlayState.IDLE)
    before = list(h.store.saves)
    h.tick(1.0)
    h.tick(1.05)
    assert h.store.saves == before
    assert h.controller._recorder.snapshot() is None
    assert h.target.values[-1] == 0.0


def test_clock_stall_does_not_bridge_recorded_coverage(h):
    h.start()
    h.tick(0.2)
    h.tick(0.25)
    h.tick(2.0, 0.30)
    h.tick(2.2, 0.50)
    h.tick(2.25, 0.55)
    learned = h.controller._recorder.snapshot()
    assert learned.value_at(0.30) is None
    assert len(learned.coverage) == 2


def test_capture_scope_loss_cannot_teach_desktop_audio(h):
    h.start()
    h.tick(0.2)
    h.tick(0.25)
    h.tick(0.3, scoped=False, level=1.0)
    h.tick(0.5, scoped=False, level=1.0)
    learned = h.controller._recorder.snapshot()
    assert learned.events[-1].media_time == pytest.approx(0.17)
    assert not h.controller._scheduled
    assert not h.controller._scoped


def test_delayed_frame_uses_its_capture_timestamp(h):
    h.start()
    h.position(0.2, 0.2)
    h.position(0.3, 0.3)
    h.frame(0.32, captured=0.25)
    h.frame(0.33, captured=0.28)
    learned = h.controller._recorder.snapshot()
    assert [e.media_time for e in learned.events] == pytest.approx([0.17, 0.20])


def test_seek_into_unmapped_territory_falls_back_without_output_jump(h):
    h.store.maps[track_key(h.current)] = flat_map(end=2.0)
    h.start()
    for i in range(4, 11):
        h.tick(i * 0.05)
    assert h.controller._scheduled
    prior = h.target.values[-1]
    h.frame(0.51, level=0.2)
    h.position(0.55, 8.0)
    assert not h.controller._scheduled
    assert h.target.values[-1] == pytest.approx(prior)
    h.tick(0.75, 8.2, level=0.2)
    assert h.target.values[-1] == pytest.approx(0.2)


def test_speed_change_drops_old_clock_bracket(h):
    h.start()
    h.learn()
    h.time(0.85)
    h.player.speed = 2.0
    h.player.speed_changed.emit(2.0)
    h.position(0.85, 0.85)
    h.frame(0.99, captured=0.84)
    h.tick(1.05, 1.25)
    h.tick(1.10, 1.35)
    learned = h.controller._recorder.snapshot()
    assert learned.value_at(0.9) is None
    assert len(learned.coverage) == 2
    assert learned.events[-1].media_time == pytest.approx(1.19)


def test_shutdown_saves_partial_listen_and_closes_store(h):
    h.start()
    h.learn()
    h.controller.shutdown()
    assert h.store.saves
    assert h.store.closed
    assert not h.controller._holding


def test_wrong_beat_grid_triggers_disagreement_despite_crossings():
    def pulse(t):
        return math.exp(-(t % 0.5) / 0.22)

    events = tuple(PulseEvent(i / 100, pulse(i / 100)) for i in range(1001))
    scheduler = PulseScheduler(PulseMap(events, ((0.0, 10.0),)))
    for i in range(1, 500):
        t = i * 0.02
        scheduler.observe(t, pulse(t + 0.25))
    assert scheduler.disagreed


@pytest.mark.parametrize("speed", [0.5, 1.0, 2.0])
def test_replay_uses_same_canonical_media_timeline_at_every_speed(h, speed):
    learned = PulseMap((PulseEvent(0.0, 0.2), PulseEvent(3.0, 0.8)), ((0.0, 3.0),))
    h.store.maps[track_key(h.current)] = learned
    h.player.speed = speed
    h.start()
    for i in range(1, round(1.0 / speed / 0.025) + 1):
        wall = i * 0.025
        media = wall * speed
        live = 0.2 + 0.2 * (media - 0.08 * speed)
        h.tick(wall, media, level=live)
    assert h.controller._scheduled
    assert not h.controller._scheduler.disagreed
    assert h.target.values[-1] == pytest.approx(0.4)


def test_ordered_state_signal_wins_over_newer_player_property(h):
    h.start()
    h.learn()
    h.player.state = PlayState.PLAYING
    h.player.state_changed.emit(PlayState.IDLE)
    assert not h.controller._holding
    assert len(h.store.saves) == 1
    h.current = track("b")
    h.player.state_changed.emit(PlayState.LOADING)
    assert not h.controller._holding
    h.player.state_changed.emit(PlayState.PLAYING)
    assert h.controller._key == track_key(h.current)


def test_seeks_in_both_directions_leave_unheard_span_unmapped(h):
    h.start()
    h.learn()
    h.tick(1.0, 5.0)
    h.tick(1.2, 5.2)
    h.tick(1.25, 5.25)
    h.tick(1.3, 0.3)
    h.tick(1.5, 0.5)
    h.tick(1.55, 0.55)
    learned = h.controller._recorder.snapshot()
    assert learned.value_at(2.0) is None
    assert learned.value_at(5.15) == pytest.approx(0.7)
    assert PulseMap.from_payload(learned.payload()) is not None


def test_live_signal_recovers_when_position_clock_stalls(h):
    h.store.maps[track_key(h.current)] = flat_map()
    h.start()
    h.learn()
    assert h.controller._scheduled
    h.frame(1.2, level=0.2)
    assert not h.controller._scheduled
    h.frame(1.4, level=0.2)
    assert h.target.values[-1] == pytest.approx(0.2)


def test_observed_disagreement_switches_controller_to_live(h):
    h.store.maps[track_key(h.current)] = flat_map(value=1.0)
    h.start()
    for i in range(4, 24):
        h.tick(i * 0.05, level=0.0)
    assert h.controller._scheduler.disagreed
    assert not h.controller._scheduled
    assert h.target.values[-1] == pytest.approx(0.0)


@pytest.mark.parametrize("mapped", [False, True])
def test_capture_stall_settles_old_live_pulse_while_clock_keeps_running(h, mapped):
    if mapped:
        h.store.maps[track_key(h.current)] = flat_map(value=0.9)
    h.start()
    for i in range(4, 17):
        h.tick(i * 0.05, level=0.9)
    assert h.target.values[-1] == pytest.approx(0.9)
    for i in range(17, 33):
        h.position(i * 0.05, i * 0.05)
    assert not h.controller._scheduled
    assert h.target.values[-1] == pytest.approx(0.0)
    learned = h.controller._recorder.snapshot()
    assert learned.events[-1].media_time == pytest.approx(0.72)


def test_capture_reset_clears_live_pulse_and_preserves_unrecorded_gap(h):
    h.start()
    h.tick(0.2)
    h.tick(0.25)
    h.time(0.3)
    h.feed.pulse_frame.emit(PulseFrame(100.3, 0.0, "sustain", 0.08, reset=True))
    assert h.target.values[-1] == pytest.approx(0.0)
    h.position(0.35, 0.35)
    h.tick(0.5)
    h.tick(0.55)
    learned = h.controller._recorder.snapshot()
    assert len(learned.coverage) == 2
    assert learned.value_at(0.3) is None
    assert learned.value_at(0.45) == pytest.approx(0.7)


def test_wrong_scope_stays_live_but_never_teaches_cached_map(h):
    h.store.maps[track_key(h.current)] = flat_map(value=0.9)
    h.start()
    for i in range(4, 21):
        h.tick(i * 0.05, level=0.6, scoped=False)
        assert not h.controller._scheduled
    assert h.target.values[-1] == pytest.approx(0.6)
    assert h.controller._recorder.snapshot() is None
    h.player.change_state(PlayState.PAUSED)
    assert h.store.saves == []


def test_construction_during_playback_bootstraps_accessor_without_state_signal(monkeypatch):
    running = Harness(monkeypatch, initial_state=PlayState.PLAYING)
    try:
        running.controller.set_pulse_enabled(True)
        assert running.store.loads[-1][1] == track_key(running.current)
        assert running.controller._holding
    finally:
        running.controller.shutdown()
