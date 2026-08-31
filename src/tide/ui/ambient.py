"""live and learned pulse sources for every registered backdrop."""
from __future__ import annotations

from bisect import bisect_right
from collections import deque
import math
import time

from PySide6.QtCore import QCoreApplication, QObject, Qt, QTimer, Slot

from .. import audio_capture, settings as settings_module
from ..pulse_map import (
    MAX_CLOCK_GAP, MAX_FRAME_GAP, PulseBlend, PulseMapStore,
    PulseRecorder, PulseScheduler, track_key,
)


_CONSUMER = "ambient"
# buffered audio around a seek/resume still belongs to the old detector baseline.
_SETTLE_SECONDS = 0.15
_CHECKPOINT_SECONDS = 30.0
_RENDER_INTERVAL_MS = 20


class AmbientController(QObject):
    def __init__(self, player, central_bg, parent: QObject | None = None,
                 *, track_provider=None) -> None:
        super().__init__(parent)
        self._player = player
        self._state = player.state
        self._central_bg = central_bg
        self._targets = [central_bg] if central_bg is not None else []
        self._track_provider = track_provider
        self._feed = audio_capture.feed()
        self._enabled = False
        self._mini_active = False
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

    def add_target(self, central_bg) -> None:
        if central_bg is not None and central_bg not in self._targets:
            self._targets.append(central_bg)
            self._reconcile()

    def remove_target(self, central_bg) -> None:
        if central_bg in self._targets:
            self._targets.remove(central_bg)
            self._set_pulse(0.0, [central_bg])
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

    def _wants_pulse(self) -> bool:
        return bool(self._targets) and (self._enabled or self._mini_active)

    def _set_pulse(self, level: float, targets=None) -> None:
        for target in (self._targets if targets is None else targets):
            try:
                target.set_pulse(level)
            except RuntimeError:
                pass

    def _live_value(self, now: float) -> float:
        return self._live if now - self._last_live_at <= MAX_CLOCK_GAP else 0.0

    def _output(self, level: float, scheduled: bool, now: float) -> None:
        self._scheduled = scheduled
        self._set_pulse(self._blend.mix(level, scheduled, now))

    def _reconcile(self) -> None:
        if self._wants_pulse() and self._is_playing():
            self._acquire()
        else:
            if not self._wants_pulse():
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
        self._render_timer.start()

    def _release(self) -> None:
        self._render_timer.stop()
        if not self._holding:
            return
        self._holding = False
        self._clock_gap(time.monotonic())
        self._feed.remove_consumer(_CONSUMER)

    def _clock_gap(self, now: float) -> None:
        self._anchors.clear()
        self._frames.clear()
        self._last_recorded_at = None
        self._recorder.gap()
        self._scheduler.gap()
        self._accept_after = now + _SETTLE_SECONDS

    def _checkpoint(self) -> None:
        if not self._dirty or self._key is None:
            return
        segments = self._recorder.segments()
        if segments:
            self._store.save_recording(self._key, segments)
        self._dirty = False
        self._last_save = time.monotonic()

    def _finish(self) -> None:
        self._checkpoint()
        self._generation += 1
        self._key = None
        self._dirty = False
        self._recorder = PulseRecorder()
        self._scheduler = PulseScheduler()
        self._clock_gap(time.monotonic())

    @Slot()
    def shutdown(self) -> None:
        self._finish()
        self._release()
        self._store.close()

    @Slot(int, str, object)
    def _on_map_loaded(self, generation: int, key: str, pulse_map) -> None:
        if generation == self._generation and key == self._key:
            self._scheduler = PulseScheduler(pulse_map)

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
        self._anchors.append((now, seconds))
        self._drain_frames()
        self._render_pulse(now)
        if now - self._last_save >= _CHECKPOINT_SECONDS:
            self._checkpoint()

    @Slot()
    def _on_render_tick(self) -> None:
        if self._holding and self._wants_pulse() and self._is_playing():
            self._render_pulse(time.monotonic())

    def _render_pulse(self, now: float) -> None:
        value = None
        if (len(self._anchors) >= 2 and self._scoped
                and now - self._frame_seen <= MAX_CLOCK_GAP
                and now >= self._accept_after):
            wall, position = self._anchors[-1]
            elapsed = now - wall
            if 0 <= elapsed <= MAX_CLOCK_GAP:
                # spotify reports every 250 ms. sample between reports, always
                # from their latest anchor; recording keeps exact brackets.
                speed = self._speed()
                value = self._scheduler.value_at(position + elapsed * speed, speed)
        self._output(value if value is not None else self._live_value(now),
                     value is not None, now)

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
            self._clock_gap(now)
            self._scoped = False
            if frame.reset or now - frame.captured_at > MAX_CLOCK_GAP:
                self._live = 0.0
            self._output(self._live_value(now), False, now)
            return
        self._scoped = True
        self._frame_seen = frame.captured_at
        self._frames.append(frame)
        self._drain_frames()
        if self._scheduler.disagreed and self._scheduled:
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
