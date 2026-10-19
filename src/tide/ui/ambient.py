"""live and learned pulse sources for every registered backdrop."""
from __future__ import annotations

from bisect import bisect_right
from collections import deque
import math
from typing import NamedTuple
import os
import time

from PySide6.QtCore import QCoreApplication, QObject, Qt, QTimer, Slot

from .. import audio_capture, settings as settings_module
from ..beat_service import BeatMapService
from ..pulse_map import (
    MATCH_WINDOW, MAX_CLOCK_GAP, MAX_FRAME_GAP, ONSET_ENTER, PulseBlend, PulseMapStore,
    PulseRecorder, PulseScheduler, track_key,
)


_CONSUMER = "ambient"
# buffered audio around a seek/resume still belongs to the old detector baseline.
_SETTLE_SECONDS = 0.15
_CHECKPOINT_SECONDS = 30.0
_RENDER_INTERVAL_MS = 16
# player clock reports are read across this much history (mpv reports every
# 100 ms, spotify every 250): long enough to hold a promptly delivered one,
# short enough that a speed or drift change is not remembered for long.
_ANCHOR_WINDOW_S = 1.0


def _beat_lead() -> float:
    """TIDE_BEAT_LEAD_MS: shift the beat-map pulse earlier (positive) or
    later (negative) to match a display's own latency. a trial knob."""
    try:
        lead = float(os.environ.get("TIDE_BEAT_LEAD_MS", "0")) / 1000.0
    except ValueError:
        return 0.0
    return lead if math.isfinite(lead) else 0.0


_BEAT_LEAD_S = _beat_lead()


class BeatClock(NamedTuple):
    """what a view drawing against the beat map reads each frame."""
    beat_map: object          # BeatMap
    position: float | None    # media seconds now; None while the clock is stopped
    speed: float


class AmbientController(QObject):
    def __init__(self, player, central_bg, parent: QObject | None = None,
                 *, track_provider=None, stream_provider=None) -> None:
        super().__init__(parent)
        self._player = player
        self._state = player.state
        self._central_bg = central_bg
        self._targets = [central_bg] if central_bg is not None else []
        self._track_provider = track_provider
        # the current track's StreamRef, when the window has one: what the
        # beat analyzer decodes. None (spotify, or not resolved yet) means
        # the map can only come from the cache.
        self._stream_provider = stream_provider
        # beat map: the track analyzed whole, scheduled against the player
        # clock. it needs no capture, so it survives capture resets and
        # runs from a single anchor; the learned map and live detection
        # stay underneath as fallbacks.
        self._beats = BeatMapService(self)
        self._beat_map = None
        self._beat_active = False
        self._feed = audio_capture.feed()
        self._enabled = False
        self._mini_active = False
        # views that read the beat clock without driving the backdrop
        # (the visualizer's deck). they keep the clock and the map alive.
        self._watchers: set[str] = set()
        # listeners that always get the pulse, whatever the backdrop
        # setting says (plugins: the vr ring). the backdrop targets stay
        # gated by adaptive_pulse.
        self._listeners: list = []
        self._holding = False
        self._key: str | None = None
        self._generation = 0
        self._recorder = PulseRecorder()
        self._scheduler = PulseScheduler()
        self._blend = PulseBlend()
        self._store = PulseMapStore(self)
        self._dirty = False
        self._last_save = 0.0
        self._live = 0.0
        self._last_live_at = -math.inf
        self._scheduled = False
        self._rescue_level = 0.0
        self._rescue_at = -math.inf
        self._rescue_release = 0.17
        self._scoped = False
        self._frame_seen = -math.inf
        self._last_recorded_at: float | None = None
        self._accept_after = 0.0
        self._anchors = deque(maxlen=64)
        self._frames = deque(maxlen=64)
        self._render_timer = QTimer(self)
        self._render_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._render_timer.setInterval(_RENDER_INTERVAL_MS)
        self._render_timer.timeout.connect(self._on_render_tick)

        self._player.state_changed.connect(self._on_state)
        self._player.position_changed.connect(self._on_position_changed)
        self._player.speed_changed.connect(self._on_speed_changed)
        self._feed.pulse_updated.connect(self._on_pulse)
        self._feed.pulse_frame.connect(self._on_frame)
        self._store.loaded.connect(self._on_map_loaded)
        self._beats.updated.connect(self._on_beat_map)
        app = QCoreApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.shutdown)

    def set_pulse_enabled(self, on: bool) -> None:
        on = bool(on)
        if on == self._enabled:
            return
        self._enabled = on
        self._reconcile()

    def set_mini_active(self, on: bool) -> None:
        on = bool(on)
        if on == self._mini_active:
            return
        self._mini_active = on
        self._reconcile()

    def set_watching(self, name: str, on: bool) -> None:
        """hold the beat map and the player clock for ``name`` while it is
        on screen, whether or not the backdrop pulses."""
        before = bool(self._watchers)
        if on:
            self._watchers.add(name)
        else:
            self._watchers.discard(name)
        if bool(self._watchers) != before:
            self._reconcile()

    def beat_clock(self) -> BeatClock | None:
        """the current track's beat map and where the playhead is on it.
        None when no map has arrived; position None while paused or
        between clock reports."""
        beat_map = self._beat_map
        if beat_map is None:
            return None
        speed = self._speed()
        position = None
        if self._holding and self._anchors and self._is_playing():
            now = time.monotonic()
            wall, _position = self._anchors[-1]
            if 0 <= now - wall <= MAX_CLOCK_GAP:
                position = self._media_at(now, speed) + _BEAT_LEAD_S * speed
        return BeatClock(beat_map, position, speed)

    def add_target(self, central_bg) -> None:
        if central_bg is not None and central_bg not in self._targets:
            self._targets.append(central_bg)
            self._reconcile()

    def remove_target(self, central_bg) -> None:
        if central_bg in self._targets:
            self._targets.remove(central_bg)
            self._set_pulse(0.0, [central_bg])
            self._reconcile()

    def add_listener(self, obj) -> None:
        """``obj.set_pulse(level)`` gets every pulse frame while music
        plays, even with the backdrop pulse off."""
        if obj is not None and obj not in self._listeners:
            self._listeners.append(obj)
            self._reconcile()

    def remove_listener(self, obj) -> None:
        if obj in self._listeners:
            self._listeners.remove(obj)
            try:
                obj.set_pulse(0.0)
            except RuntimeError:
                pass
            self._reconcile()

    def is_enabled(self) -> bool:
        return self._enabled

    def _is_playing(self) -> bool:
        from ..player import PlayState
        return self._state == PlayState.PLAYING

    def _speed(self) -> float:
        supports = getattr(self._player, "active_supports_speed", None)
        if supports is not None and not supports():
            return 1.0
        return max(0.25, min(4.0, float(self._player.speed)))

    @Slot(object)
    def _on_state(self, state) -> None:
        from ..player import PlayState
        self._state = state
        if state in (PlayState.IDLE, PlayState.LOADING):
            self._finish()
        elif state == PlayState.PAUSED:
            self._checkpoint()
        self._reconcile()

    def _backdrop_pulses(self) -> bool:
        return bool(self._targets) and (self._enabled or self._mini_active)

    def _wants_pulse(self) -> bool:
        return self._backdrop_pulses() or bool(self._listeners)

    def _wants_clock(self) -> bool:
        return self._wants_pulse() or bool(self._watchers)

    def _set_pulse(self, level: float, targets=None) -> None:
        if targets is None:
            targets = (self._targets if self._backdrop_pulses() else []) + self._listeners
        for target in targets:
            try:
                target.set_pulse(level)
            except RuntimeError:
                pass

    def _live_value(self, now: float) -> float:
        return self._live if now - self._last_live_at <= MAX_CLOCK_GAP else 0.0

    def _output(self, level: float, scheduled: bool, now: float,
                source=None) -> None:
        # the blend crossfades whenever the source changes: live, learned
        # map, beat map, and (for the beat map) each newer map object
        self._scheduled = scheduled
        if not self._wants_pulse():
            return              # only watchers hold the clock: the backdrop stays still
        mixed = self._blend.mix(level, scheduled if source is None else source, now)
        self._set_pulse(max(mixed, self._rescue_value(now)))

    def _rescue_value(self, now: float) -> float:
        age = max(0.0, now - self._rescue_at)
        if age > 6 * self._rescue_release:
            return 0.0
        return self._rescue_level * math.exp(-age / self._rescue_release)

    def _rescue_hit(self, frame, now: float) -> None:
        pulse_map = self._scheduler.pulse_map
        if (not self._scheduled or self._beat_active
                or pulse_map is None or pulse_map.hits is None
                or not self._anchors or frame.onset is None
                or not math.isfinite(frame.onset) or frame.onset < ONSET_ENTER
                or frame.kind == "sustain" or not math.isfinite(frame.level)
                or not math.isfinite(frame.latency)
                or not 0 <= now - frame.captured_at <= MAX_FRAME_GAP):
            return
        wall, position = self._anchors[-1]
        if not 0 <= now - wall <= MAX_CLOCK_GAP:
            return
        speed = self._speed()
        position += (frame.captured_at - wall - frame.latency) * speed
        # a cached hit already landed on time. its delayed capture must not
        # fire it a second time; only rescue attacks missing from the map.
        times = pulse_map._hit_times
        index = bisect_right(times, position)
        if any(abs(times[i] - position) <= MATCH_WINDOW * speed
               for i in (index - 1, index) if 0 <= i < len(times)):
            return
        self._rescue_level = max(self._rescue_value(frame.captured_at),
                                 max(0.0, min(1.0, frame.level)))
        self._rescue_at = frame.captured_at
        self._rescue_release = 0.17 if frame.kind == "kick" else 0.11
        # don't wait for the next player-clock bracket or the map's three
        # conflicts: the listener heard this attack already.
        self._render_pulse(now)

    def _reconcile(self) -> None:
        if self._wants_clock() and self._is_playing():
            self._acquire()
            if self._wants_pulse():
                self._render_timer.start()
            else:
                self._render_timer.stop()
                self._blend = PulseBlend()
                self._set_pulse(0.0)
        else:
            if not self._wants_clock():
                self._finish()
            self._release()
            self._blend = PulseBlend()
            self._scheduled = False
            self._set_pulse(0.0)

    def _begin(self) -> None:
        track = self._track_provider() if self._track_provider is not None else None
        key = track_key(track)
        if key == self._key:
            return
        self._finish()
        self._key = key
        self._last_save = time.monotonic()
        if key is not None:
            self._store.load(self._generation, key)
            self._request_beats(key, urgent=True)

    def _stream_ref(self):
        if self._stream_provider is None:
            return None
        try:
            return self._stream_provider()
        except Exception:
            return None

    def _request_beats(self, key: str, *, urgent: bool, ref=None) -> None:
        if ref is None:
            ref = self._stream_ref()
        payload = headers = None
        if ref is not None and getattr(ref, "backend", "mpv") == "mpv":
            payload = getattr(ref, "payload", None)
            headers = getattr(ref, "headers", None)
        self._beats.request(key, payload, headers, urgent=urgent)

    def prefetch_beats(self, track, ref) -> None:
        """analyze an upcoming track now, so its map is ready when it
        starts. called by the window when a prefetch resolves."""
        key = track_key(track)
        if key is not None and key != self._key:
            self._request_beats(key, urgent=False, ref=ref)

    def _acquire(self) -> None:
        if self._holding:
            return
        self._begin()
        self._clock_gap(time.monotonic())
        self._scoped = False
        source = None
        try:
            source = settings_module.load().audio_device or None
        except Exception:
            pass
        self._feed.add_consumer(_CONSUMER, source=source)
        self._holding = True

    def _release(self) -> None:
        self._render_timer.stop()
        if not self._holding:
            return
        self._holding = False
        self._clock_gap(time.monotonic())
        self._feed.remove_consumer(_CONSUMER)

    def _clock_gap(self, now: float) -> None:
        self._anchors.clear()
        self._learn_gap(now)

    def _learn_gap(self, now: float) -> None:
        """reset everything that learns from or validates against capture,
        leaving the player-clock anchors alone."""
        self._rescue_level = 0.0
        self._rescue_at = -math.inf
        self._frames.clear()
        self._last_recorded_at = None
        self._recorder.gap()
        self._scheduler.gap()
        self._accept_after = now + _SETTLE_SECONDS

    def _checkpoint(self, *, final: bool = False) -> None:
        if not self._dirty or self._key is None:
            return
        # deltas only: resubmitting the whole recording made every 30s
        # checkpoint O(track-so-far) on both sides of the store queue.
        segments = self._recorder.drain_pending(final=final)
        if segments:
            self._store.save_recording(self._key, segments)
        self._dirty = self._recorder.has_pending()
        self._last_save = time.monotonic()

    def _finish(self) -> None:
        suspect = 0.0
        probe = getattr(self._player, "capture_suspect_window", None)
        if probe is not None:
            try:
                suspect = float(probe())
            except Exception:
                suspect = 0.0
        if suspect > 0:
            # a poll-based backend noticed the track change late; the tail
            # of the recording is the next track's audio under this key.
            self._recorder.discard_tail(suspect)
        self._checkpoint(final=True)
        self._generation += 1
        self._key = None
        self._dirty = False
        self._recorder = PulseRecorder()
        self._scheduler = PulseScheduler()
        self._beat_map = None
        self._beat_active = False
        self._clock_gap(time.monotonic())

    @Slot()
    def shutdown(self) -> None:
        self._finish()
        self._release()
        self._store.close()
        self._beats.close()

    @Slot(int, str, object)
    def _on_map_loaded(self, generation: int, key: str, pulse_map) -> None:
        if generation == self._generation and key == self._key:
            self._scheduler = PulseScheduler(pulse_map)

    @Slot(str, object)
    def _on_beat_map(self, key: str, beat_map) -> None:
        # partial maps arrive while the decode runs; each one supersedes
        # the last. the next render tick picks it up.
        if key == self._key and beat_map is not None:
            self._beat_map = beat_map

    @Slot(float)
    def _on_speed_changed(self, _speed: float) -> None:
        self._clock_gap(time.monotonic())
        if self._holding:
            self._output(self._live_value(time.monotonic()), False, time.monotonic())

    @Slot(float)
    def _on_position_changed(self, seconds: float) -> None:
        if not self._holding or not self._is_playing() or not math.isfinite(seconds):
            return
        now = time.monotonic()
        speed = self._speed()
        if self._anchors:
            wall, position = self._anchors[-1]
            elapsed, advance = now - wall, seconds - position
            if (elapsed <= 0 or elapsed > MAX_CLOCK_GAP or advance <= 0
                    or abs(advance - elapsed * speed) > max(0.035, elapsed * speed * 0.5)):
                self._clock_gap(now)
                # every present track change bounces through LOADING, but a
                # backend-initiated one (gapless, remote skip) would first
                # show as a clock break; rekey here so scoped frames of the
                # new audio cannot keep teaching the old track.
                self._begin()
        self._anchors.append((now, seconds))
        self._drain_frames()
        self._render_pulse(now)
        if now - self._last_save >= _CHECKPOINT_SECONDS:
            self._checkpoint()

    def _media_at(self, now: float, speed: float) -> float:
        """the media position now. an anchor is stamped when the gui thread
        gets to it, never before the player read it, so a busy frame only
        ever makes an anchor read late: of the last second's anchors, the
        one that puts the playhead furthest along was delivered soonest."""
        best = -math.inf
        for wall, position in reversed(self._anchors):
            age = now - wall
            if age > _ANCHOR_WINDOW_S:
                break
            if age >= 0:
                best = max(best, position + age * speed)
        if best == -math.inf:
            wall, position = self._anchors[-1]
            best = position + (now - wall) * speed
        return best

    @Slot()
    def _on_render_tick(self) -> None:
        if self._holding and self._wants_pulse() and self._is_playing():
            self._render_pulse(time.monotonic())

    def _render_pulse(self, now: float) -> None:
        value = None
        source = None
        if self._anchors:
            wall, position = self._anchors[-1]
            elapsed = now - wall
            if 0 <= elapsed <= MAX_CLOCK_GAP:
                # spotify reports every 250 ms. sample between reports, from
                # the freshest-reading anchor; recording keeps exact brackets.
                speed = self._speed()
                media = self._media_at(now, speed)
                if self._beat_map is not None:
                    # the map was made from the stream itself, so one
                    # anchor is enough and capture state is irrelevant
                    value = self._beat_map.value_at(media + _BEAT_LEAD_S * speed, speed)
                    if value is not None:
                        source = ("beat", id(self._beat_map))
                if (value is None and len(self._anchors) >= 2 and self._scoped
                        and now - self._frame_seen <= MAX_CLOCK_GAP
                        and now >= self._accept_after):
                    value = self._scheduler.value_at(media, speed)
                    if value is not None:
                        source = "map"
        self._beat_active = source is not None and source != "map"
        if value is None:
            self._output(self._live_value(now), False, now)
        else:
            self._output(value, True, now, source)

    @Slot(float)
    def _on_pulse(self, level: float) -> None:
        if not self._holding or not self._wants_pulse() or not math.isfinite(level):
            return
        self._live = max(0.0, min(1.0, level))
        now = time.monotonic()
        self._last_live_at = now
        clock_stale = not self._anchors or now - self._anchors[-1][0] > MAX_CLOCK_GAP
        if not self._scheduled or clock_stale:
            self._output(self._live_value(now), False, now)

    @Slot(object)
    def _on_frame(self, frame) -> None:
        if not self._holding or not self._wants_pulse():
            return
        now = time.monotonic()
        if frame.captured_at < self._accept_after:
            return
        if frame.reset or not frame.scoped or now - frame.captured_at > MAX_CLOCK_GAP:
            if self._beat_map is None:
                # the learned replay may not resume from an old anchor
                self._clock_gap(now)
            else:
                # the beat map owes capture nothing; keep the clock
                self._learn_gap(now)
            self._scoped = False
            if frame.reset or now - frame.captured_at > MAX_CLOCK_GAP:
                self._live = 0.0
            if not self._beat_active:
                self._output(self._live_value(now), False, now)
            return
        self._scoped = True
        self._frame_seen = frame.captured_at
        self._rescue_hit(frame, now)
        self._frames.append(frame)
        self._drain_frames()
        if self._scheduler.disagreed and self._scheduled and not self._beat_active:
            self._output(self._live_value(now), False, now)

    def _drain_frames(self) -> None:
        if len(self._anchors) < 2:
            return
        walls = [wall for wall, _position in self._anchors]
        while self._frames and self._frames[0].captured_at <= walls[-1]:
            frame = self._frames.popleft()
            index = bisect_right(walls, frame.captured_at) - 1
            if index < 0:
                self._recorder.gap()
                continue
            if index == len(walls) - 1:
                index -= 1
            left_wall, left_pos = self._anchors[index]
            right_wall, right_pos = self._anchors[index + 1]
            rate = (right_pos - left_pos) / (right_wall - left_wall)
            # the frame carries its emission timestamp, so GUI delivery delay
            # is removed without guessing. the capture correction is in wall
            # seconds; normalize it at the rate used for this listen.
            position = (left_pos + (frame.captured_at - left_wall) * rate
                        - frame.latency * self._speed())
            if (self._last_recorded_at is None
                    or frame.captured_at - self._last_recorded_at > MAX_FRAME_GAP):
                self._recorder.gap()
                self._scheduler.gap()
            self._last_recorded_at = frame.captured_at
            if self._key is not None:
                self._recorder.record(position, frame.level, frame.kind,
                                      onset=frame.onset, energy=frame.energy,
                                      speed=self._speed())
                self._dirty = True
            self._scheduler.observe(position, frame.level, self._speed(),
                                    onset=frame.onset)
