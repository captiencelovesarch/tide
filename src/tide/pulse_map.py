"""learned pulse envelopes in media seconds."""
from __future__ import annotations

import base64
from bisect import bisect_right
from dataclasses import dataclass, field
import json
import math
import queue
import threading
import zlib

from PySide6.QtCore import QObject, Signal

from . import cache


NAMESPACE = "pulse-maps-v1"
TTL_SECONDS = 30 * 24 * 3600
MAX_EVENTS = 200_000
MAX_PAYLOAD_BYTES = 12 * 1024 * 1024
KINDS = frozenset(("kick", "beat", "sustain"))
# longer gaps can be a seek or a stalled backend. Never fill them by guessing.
MAX_CLOCK_GAP = 0.35
MAX_FRAME_GAP = 0.10
CROSSFADE_SECONDS = 0.12


def track_key(track) -> str | None:
    vid = getattr(track, "video_id", None)
    if not isinstance(vid, str) or not vid:
        return None
    return json.dumps([getattr(track, "source", "") or "ytmusic", vid],
                      separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class PulseEvent:
    media_time: float
    strength: float
    kind: str = "kick"


@dataclass(frozen=True, slots=True)
class PulseMap:
    events: tuple[PulseEvent, ...]
    coverage: tuple[tuple[float, float], ...]
    _times: tuple[float, ...] = field(init=False, repr=False)
    _starts: tuple[float, ...] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_times", tuple(e.media_time for e in self.events))
        object.__setattr__(self, "_starts", tuple(a for a, _b in self.coverage))

    def value_at(self, position: float) -> float | None:
        if not math.isfinite(position):
            return None
        span = bisect_right(self._starts, position) - 1
        if span < 0 or position > self.coverage[span][1]:
            return None
        idx = bisect_right(self._times, position) - 1
        if idx < 0 or self._times[idx] < self.coverage[span][0]:
            return None
        event = self.events[idx]
        # interpolation preserves the recorded release without another envelope
        # follower. Neither endpoint may cross an unrecorded stretch.
        if idx + 1 < len(self.events):
            nxt = self.events[idx + 1]
            if nxt.media_time <= self.coverage[span][1]:
                fraction = (position - event.media_time) / (nxt.media_time - event.media_time)
                return event.strength + fraction * (nxt.strength - event.strength)
        return event.strength

    def payload(self) -> dict:
        # the JSON store rewrites its namespace. Compress the dense trace so
        # a library of replays doesn't turn each checkpoint into megabytes.
        raw = json.dumps([[e.media_time, round(e.strength, 5), e.kind]
                          for e in self.events], separators=(",", ":")).encode()
        return {"version": 1, "coverage": self.coverage,
                "trace": base64.b64encode(zlib.compress(raw)).decode("ascii")}

    @classmethod
    def from_payload(cls, payload) -> PulseMap | None:
        try:
            if not isinstance(payload, dict) or payload.get("version") != 1:
                return None
            encoded = payload["trace"]
            if not isinstance(encoded, str) or len(encoded) > MAX_PAYLOAD_BYTES:
                return None
            decoder = zlib.decompressobj()
            raw = decoder.decompress(base64.b64decode(encoded, validate=True),
                                     MAX_PAYLOAD_BYTES + 1)
            if len(raw) > MAX_PAYLOAD_BYTES or not decoder.eof:
                return None
            rows = json.loads(raw)
            if not isinstance(rows, list) or not 2 <= len(rows) <= MAX_EVENTS:
                return None
            events = tuple(PulseEvent(float(t), float(v), k) for t, v, k in rows)
            spans = tuple((float(a), float(b)) for a, b in payload["coverage"])
            if not spans or len(spans) > len(events):
                return None
            previous = -1.0
            for e in events:
                if (not math.isfinite(e.media_time) or e.media_time <= previous
                        or not math.isfinite(e.strength) or not 0 <= e.strength <= 1
                        or e.kind not in KINDS):
                    return None
                previous = e.media_time
            previous = -1.0
            for a, b in spans:
                if (not math.isfinite(a) or not math.isfinite(b)
                        or a < 0 or a <= previous or b <= a):
                    return None
                previous = b
            result = cls(events, spans)
            if any(result.value_at(a) is None or result.value_at(b) is None
                   for a, b in spans):
                return None
            if events[0].media_time < spans[0][0] or events[-1].media_time > spans[-1][1]:
                return None
            return result
        except (ValueError, TypeError, KeyError, OverflowError, zlib.error):
            return None


def merge_maps(old: PulseMap | None, new: PulseMap) -> PulseMap:
    if old is None:
        return new
    # new observations replace only the spans actually heard. A seek must
    # never erase the rest of an earlier listen, or bridge its holes.
    spans = list(old.coverage)
    for a, b in new.coverage:
        remaining = []
        for x, y in spans:
            if b < x or a > y:
                remaining.append((x, y))
            else:
                if x < a:
                    remaining.append((x, math.nextafter(a, -math.inf)))
                if b < y:
                    remaining.append((math.nextafter(b, math.inf), y))
        spans = remaining
    events = [e for e in old.events if new.value_at(e.media_time) is None]
    # retained tails need their boundary values when the old samples straddle
    # a newly observed span. Otherwise a cache merge invents a tiny hole.
    for a, b in spans:
        for t in (a, b):
            value = old.value_at(t)
            if value is not None:
                events.append(PulseEvent(t, value, "sustain"))
    events.extend(new.events)
    by_time = {e.media_time: e for e in events}
    ordered = tuple(by_time[t] for t in sorted(by_time))
    if len(ordered) > MAX_EVENTS:
        return old
    joined: list[tuple[float, float]] = []
    for a, b in sorted(spans + list(new.coverage)):
        if joined and a <= math.nextafter(joined[-1][1], math.inf):
            joined[-1] = (joined[-1][0], max(joined[-1][1], b))
        else:
            joined.append((a, b))
    return PulseMap(ordered, tuple(joined))


class PulseRecorder:
    def __init__(self) -> None:
        self._segments: list[list[PulseEvent]] = []
        self._segment: list[PulseEvent] | None = None
        self._count = 0

    def gap(self) -> None:
        self._segment = None

    def record(self, position: float, level: float, kind: str = "kick") -> None:
        if (not math.isfinite(position) or position < 0
                or not math.isfinite(level) or not 0 <= level <= 1
                or kind not in KINDS or self._count >= MAX_EVENTS):
            self.gap()
            return
        if self._segment and position <= self._segment[-1].media_time:
            self.gap()
        if self._segment is None:
            self._segment = []
            self._segments.append(self._segment)
        self._segment.append(PulseEvent(position, level, kind))
        self._count += 1

    def segments(self) -> tuple[tuple[PulseEvent, ...], ...]:
        return tuple(tuple(segment) for segment in self._segments if len(segment) >= 2)

    def snapshot(self) -> PulseMap | None:
        return map_from_segments(self.segments())


def map_from_segments(segments) -> PulseMap | None:
    result = None
    for segment in segments:
        part = PulseMap(segment, ((segment[0].media_time, segment[-1].media_time),))
        result = merge_maps(result, part)
    return result


class PulseScheduler:
    def __init__(self, pulse_map: PulseMap | None = None) -> None:
        self.pulse_map = pulse_map
        self.disagreed = False
        self._bad_time = 0.0
        self._last_observation: float | None = None

    def value_at(self, position: float) -> float | None:
        if self.disagreed or self.pulse_map is None:
            return None
        return self.pulse_map.value_at(position)

    def gap(self) -> None:
        self._bad_time = 0.0
        self._last_observation = None

    def observe(self, position: float, level: float, speed: float = 1.0) -> None:
        if not all(math.isfinite(v) for v in (position, level, speed)) or speed <= 0:
            self.gap()
            return
        expected = self.value_at(position)
        if expected is None:
            self.gap()
            return
        elapsed = 0.0
        if self._last_observation is not None:
            elapsed = (position - self._last_observation) / speed
            if elapsed <= 0 or elapsed > MAX_FRAME_GAP:
                self.gap()
                elapsed = 0.0
        self._last_observation = position
        if abs(expected - level) > 0.25:
            self._bad_time += elapsed
        else:
            # wrong beat grids cross each other twice per beat. A crossing
            # must not erase all the disagreement on either side of it.
            self._bad_time = max(0.0, self._bad_time - elapsed * 0.5)
        if self._bad_time >= 0.30:
            self.disagreed = True


class PulseBlend:
    def __init__(self) -> None:
        self.level = 0.0
        self._scheduled = False
        self._offset = 0.0
        self._changed_at = 0.0

    def mix(self, level: float, scheduled: bool, now: float) -> float:
        if scheduled != self._scheduled:
            self._offset = self.level - level
            self._changed_at = now
            self._scheduled = scheduled
        fraction = min(1.0, max(0.0, (now - self._changed_at) / CROSSFADE_SECONDS))
        self.level = max(0.0, min(1.0, level + self._offset * (1.0 - fraction)))
        return self.level


class PulseMapStore(QObject):
    loaded = Signal(int, str, object)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._jobs = queue.Queue()
        self._thread: threading.Thread | None = None
        self._closed = False

    def _submit(self, job) -> None:
        if self._closed:
            return
        self._jobs.put(job)
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="tide-pulse-cache", daemon=True)
            self._thread.start()

    def load(self, generation: int, key: str) -> None:
        self._submit(("load", generation, key, None))

    def save(self, key: str, pulse_map: PulseMap) -> None:
        self._submit(("save", 0, key, pulse_map))

    def save_recording(self, key: str, segments) -> None:
        self._submit(("recording", 0, key, segments))

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._thread is not None:
            self._jobs.put(None)
            self._thread.join(timeout=1.0)

    def _run(self) -> None:
        while (job := self._jobs.get()) is not None:
            operation, generation, key, pulse_map = job
            try:
                old = PulseMap.from_payload(cache.get_json(NAMESPACE, key))
                if operation == "recording":
                    pulse_map = map_from_segments(pulse_map)
                if operation in ("save", "recording") and pulse_map is not None:
                    merged = merge_maps(old, pulse_map)
                    cache.put_json(NAMESPACE, key, merged.payload(), TTL_SECONDS)
                elif operation == "load" and not self._closed:
                    self.loaded.emit(generation, key, old)
            except Exception:
                # an unwritable cache must leave the live detector usable.
                if operation == "load" and not self._closed:
                    self.loaded.emit(generation, key, None)
