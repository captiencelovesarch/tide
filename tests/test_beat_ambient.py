"""the beat map drives the pulse from the player clock alone."""
from types import SimpleNamespace

import pytest

from tide.beat_map import Beat, BeatMap
from tide.player import PlayState
from tide.pulse_map import track_key
from test_pulse_clock import h  # noqa: F401  shared player/capture clock harness


KEY = track_key(SimpleNamespace(source="spotify", video_id="clock-song"))


def beat_map(*times, strength=1.0, duration=10.0, swell=1.0, period=0.5):
    beats = tuple(Beat(t, strength, i % 4, period) for i, t in enumerate(times))
    return BeatMap(duration, True, 60.0 / period, 1.0, beats, (),
                   (swell,) * (int(duration * 4) + 2))


def test_starting_a_track_asks_for_its_beats_with_the_stream(h):
    h.stream = SimpleNamespace(backend="mpv", payload="https://cdn.example/a.webm",
                               headers={"User-Agent": "x"})
    h.start()
    assert h.beats.requests[-1] == (KEY, "https://cdn.example/a.webm", {"User-Agent": "x"}, True)


def test_spotify_or_unresolved_stream_is_cache_only(h):
    h.stream = SimpleNamespace(backend="librespot", payload="spotify:track:1")
    h.start()
    assert h.beats.requests[-1] == (KEY, None, None, True)


def test_prefetch_requests_are_not_urgent_and_skip_the_current_track(h):
    h.start()
    h.controller.prefetch_beats(SimpleNamespace(source="ytmusic", video_id="next"),
                                SimpleNamespace(backend="mpv", payload="/tmp/next.mp3", headers=None))
    assert h.beats.requests[-1] == (track_key(SimpleNamespace(source="ytmusic", video_id="next")),
                                    "/tmp/next.mp3", None, False)
    n = len(h.beats.requests)
    h.controller.prefetch_beats(SimpleNamespace(source="spotify", video_id="clock-song"),
                                SimpleNamespace(backend="mpv", payload="/tmp/cur.mp3", headers=None))
    assert len(h.beats.requests) == n


def test_beat_map_schedules_from_one_anchor_without_any_capture(h):
    h.start()                       # one anchor at media 0, no frames ever
    h.beats.updated.emit(KEY, beat_map(0.3))
    h.render(0.05)                  # first render starts the crossfade in
    assert h.controller._beat_active
    h.render(0.20)
    assert h.samples[-1] == pytest.approx((0.20, 0.25))       # swell floor
    h.render(0.30)
    assert h.controller._beat_active
    assert h.controller._scheduled
    assert h.samples[-1][1] == pytest.approx(1.0, abs=0.02)   # peak on the beat
    h.render(0.34)
    assert 0.5 < h.samples[-1][1] < 1.0


def test_beat_map_wins_over_learned_map_and_live_frames(h):
    h.start()
    h.drive()                       # learned map replaying at 0.8
    assert h.controller._scheduled and not h.controller._beat_active
    h.beats.updated.emit(KEY, beat_map(2.0, swell=0.0))
    h.render(0.90)
    assert h.controller._beat_active
    h.frame(0.92, 0.9)              # live says loud; the map says dark
    h.render(0.94)
    # crossfade from the learned 0.8 toward the map's 0.0 is under way
    assert h.samples[-1][1] < 0.7
    h.position(1.0, 1.0)
    h.render(1.20)
    assert h.controller._beat_active
    assert h.samples[-1][1] == pytest.approx(0.0, abs=0.01)


def test_outside_map_coverage_falls_back_to_live(h):
    h.start()
    h.beats.updated.emit(KEY, beat_map(0.5, duration=1.0))
    h.position(0.25, 0.25)
    h.render(0.50)
    assert h.controller._beat_active
    h.position(1.5, 1.5)
    h.frame(1.52, 0.6)
    h.render(1.53)
    assert not h.controller._beat_active
    assert not h.controller._scheduled
    assert h.samples[-1][1] > 0.4


def test_capture_reset_keeps_the_clock_while_a_beat_map_plays(h):
    h.start()
    h.beats.updated.emit(KEY, beat_map(0.5))
    h.render(0.20)
    h.position(0.25, 0.25)
    h.frame(0.27, 0.2, reset=True)
    assert len(h.controller._anchors) == 2
    h.render(0.50)
    assert h.controller._beat_active
    assert h.samples[-1][1] == pytest.approx(1.0, abs=0.02)


def test_capture_reset_still_clears_the_clock_without_a_beat_map(h):
    h.start()
    h.frame(0.22, 0.2, reset=True)
    assert not h.controller._anchors


def test_rescue_hits_are_suppressed_under_a_beat_map(h):
    h.start()
    h.beats.updated.emit(KEY, beat_map(5.0))
    h.render(0.30)
    assert h.controller._beat_active
    h.frame(0.32, 0.9, onset=0.9, latency=0.02)
    assert h.controller._rescue_level == 0.0


def test_maps_for_other_tracks_and_finished_tracks_are_dropped(h):
    h.start()
    h.beats.updated.emit("other", beat_map(0.5))
    assert h.controller._beat_map is None
    h.beats.updated.emit(KEY, beat_map(0.5))
    assert h.controller._beat_map is not None
    h.player.change_state(PlayState.IDLE)
    assert h.controller._beat_map is None
    assert not h.controller._beat_active


def test_newer_map_replaces_older_one(h):
    h.start()
    first = beat_map(0.5)
    h.beats.updated.emit(KEY, first)
    later = beat_map(0.5, 1.0)
    h.beats.updated.emit(KEY, later)
    assert h.controller._beat_map is later


def test_shutdown_closes_the_service(h):
    h.start()
    h.controller.shutdown()
    assert h.beats.closed
