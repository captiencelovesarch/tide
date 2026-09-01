"""learned pulse events and their live-audio reference."""
from __future__ import annotations

import base64
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field, replace
import json
import math
import queue
import statistics
import threading
import zlib

from PySide6.QtCore import QObject, Signal

from . import cache
from .audio_capture import PULSE_SUSTAIN_FLOOR


NAMESPACE = "pulse-maps-v2"
TTL_SECONDS = 30 * 24 * 3600
MAX_EVENTS = 200_000
MAX_HITS = 50_000
MAX_PAYLOAD_BYTES = 12 * 1024 * 1024
KINDS = frozenset(("kick", "beat", "sustain"))
MAX_CLOCK_GAP = 0.35
MAX_FRAME_GAP = 0.10
CROSSFADE_SECONDS = 0.12
# two detector frames can belong to one attack. these are wall seconds.
ONSET_ENTER = 0.20
ONSET_LEAVE = 0.08
HIT_DEBOUNCE = 0.050
HIT_HOLD = 0.025
MATCH_WINDOW = 0.060
# recorded-silence vs live-bass disagreement. a release tail passes through
# low values on its way down, so silence only counts when the map was also
# quiet a stretch earlier; 0.6 covers the tail's descent at 1x record speed
# and the look-back disqualifies itself for slower tails at higher speeds.
SILENCE_LEVEL = 0.05
SILENCE_LOOKBACK = 0.6


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
class PulseSample(PulseEvent):
    onset: float | None = None
    energy: float | None = None
    speed: float = 1.0


@dataclass(frozen=True, slots=True)
class PulseHit(PulseEvent):
    confidence: float = 1.0
    energy: float | None = None
    period: float | None = None
    rhythm_confidence: float = 0.0


def _energy_reference(hits) -> float:
    energies = sorted(h.energy for h in hits if h.energy is not None and h.energy > 0)
    if not energies:
        return 0.0
    index = 0.9 * (len(energies) - 1)
    low = int(index)
    return energies[low] + (index - low) * (energies[min(low + 1, len(energies) - 1)] - energies[low])


@dataclass(frozen=True, slots=True)
class PulseMap:
    events: tuple[PulseEvent, ...]
    coverage: tuple[tuple[float, float], ...]
    hits: tuple[PulseHit, ...] | None = None
    _times: tuple[float, ...] = field(init=False, repr=False)
    _starts: tuple[float, ...] = field(init=False, repr=False)
    _hit_times: tuple[float, ...] = field(init=False, repr=False)
    _hit_levels: tuple[float, ...] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_times", tuple(e.media_time for e in self.events))
        object.__setattr__(self, "_starts", tuple(a for a, _b in self.coverage))
        hits = self.hits or ()
        reference = _energy_reference(hits)
        # one common gain change cancels in this ratio. an in-song volume
        # change remains indistinguishable from dynamics in captured PCM.
        levels = tuple(h.strength * (
            max(0.25, min(1.0, (h.energy / reference) ** 0.35))
            if h.energy is not None and reference > 0 else 1.0) for h in hits)
        object.__setattr__(self, "_hit_times", tuple(h.media_time for h in hits))
        object.__setattr__(self, "_hit_levels", levels)

    def _span(self, position: float) -> int:
        if not math.isfinite(position):
            return -1
        index = bisect_right(self._starts, position) - 1
        return index if index >= 0 and position <= self.coverage[index][1] else -1

    def reference_at(self, position: float) -> float | None:
        span = self._span(position)
        if span < 0:
            return None
        idx = bisect_right(self._times, position) - 1
        if idx < 0 or self._times[idx] < self.coverage[span][0]:
            return None
        event = self.events[idx]
        if idx + 1 < len(self.events):
            nxt = self.events[idx + 1]
            if nxt.media_time <= self.coverage[span][1]:
                fraction = (position - event.media_time) / (nxt.media_time - event.media_time)
                return event.strength + fraction * (nxt.strength - event.strength)
        return event.strength

    def value_at(self, position: float, speed: float = 1.0) -> float | None:
        if not math.isfinite(speed) or speed <= 0:
            return None
        if self.hits is None:
            return self.reference_at(position)
        span = self._span(position)
        if span < 0:
            return None
        end = bisect_right(self._hit_times, position)
        start = max(end - 32, bisect_left(
            self._hit_times, max(self.coverage[span][0], position - 1.2 * speed)))
        # the live detector breathes up to the sustain ceiling on onset-free
        # bass; without this floor a replayed drone passage goes dark.
        reference = self.reference_at(position)
        level = min(reference, PULSE_SUSTAIN_FLOOR) if reference is not None else 0.0
        for index in range(start, end):
            hit = self.hits[index]
            age = (position - hit.media_time) / speed
            release = 0.17 if hit.kind == "kick" else 0.11
            if hit.period is not None and hit.rhythm_confidence >= 0.75:
                release = min(release, max(0.065, hit.period / speed * 0.28))
            if age <= HIT_HOLD + 6 * release:
                shaped = self._hit_levels[index] * math.exp(-max(0.0, age - HIT_HOLD) / release)
                level = max(level, shaped)
        return level

    def payload(self) -> dict:
        raw = json.dumps([[e.media_time, round(e.strength, 5), e.kind]
                          for e in self.events], separators=(",", ":")).encode()
        # times keep full precision — merge boundaries are nextafter-spaced
        # and rounding would collapse them into an invalid payload.
        hits = None if self.hits is None else [
            [h.media_time, round(h.strength, 5), h.kind, round(h.confidence, 5),
             None if h.energy is None else round(h.energy, 6),
             None if h.period is None else round(h.period, 6),
             round(h.rhythm_confidence, 5)] for h in self.hits]
        return {"version": 2, "coverage": self.coverage, "hits": hits,
                "trace": base64.b64encode(zlib.compress(raw)).decode("ascii")}

    @classmethod
    def from_payload(cls, payload) -> PulseMap | None:
        try:
            # v1 stored shaped detector output without attack evidence.
            # treating its bright decay samples as hits would invent a roll.
            if not isinstance(payload, dict) or payload.get("version") != 2:
                return None
            encoded = payload["trace"]
            if not isinstance(encoded, str) or len(encoded) > MAX_PAYLOAD_BYTES:
                return None
            decoder = zlib.decompressobj()
            raw = decoder.decompress(base64.b64decode(encoded, validate=True), MAX_PAYLOAD_BYTES + 1)
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
            hit_rows = payload.get("hits")
            hits = None
            if hit_rows is not None:
                if not isinstance(hit_rows, list) or len(hit_rows) > MAX_HITS:
                    return None
                hits = tuple(PulseHit(float(t), float(v), k, float(c),
                                      None if e is None else float(e),
                                      None if p is None else float(p), float(r))
                             for t, v, k, c, e, p, r in hit_rows)
                previous = -1.0
                for h in hits:
                    if (not all(math.isfinite(v) for v in (h.media_time, h.strength, h.confidence, h.rhythm_confidence))
                            or h.media_time <= previous or not 0 <= h.strength <= 1
                            or not 0 <= h.confidence <= 1 or not 0 <= h.rhythm_confidence <= 1
                            or h.kind not in ("kick", "beat")
                            or (h.energy is not None and (not math.isfinite(h.energy) or h.energy < 0))
                            or (h.period is not None and (not math.isfinite(h.period) or not 0.1 <= h.period <= 5))):
                        return None
                    previous = h.media_time
            result = cls(events, spans, hits)
            if any(result.reference_at(a) is None or result.reference_at(b) is None for a, b in spans):
                return None
            if events[0].media_time < spans[0][0] or events[-1].media_time > spans[-1][1]:
                return None
            if hits is not None and any(result.reference_at(h.media_time) is None for h in hits):
                return None
            return result
        except (ValueError, TypeError, KeyError, OverflowError, zlib.error):
            return None


def _rhythm_hints(hits, coverage) -> tuple[PulseHit, ...]:
    result = []
    previous = []
    span = -1
    starts = [a for a, _b in coverage]
    for hit in hits:
        current = bisect_right(starts, hit.media_time) - 1
        if current != span:
            previous = []
            span = current
        period, confidence = None, 0.0
        if hit.confidence >= 0.55:
            previous.append(hit.media_time)
            previous = previous[-13:]
            if len(previous) >= 6:
                intervals = [b - a for a, b in zip(previous, previous[1:])]
                candidate = statistics.median(intervals)
                if 0.28 <= candidate <= 1.2:
                    confidence = sum(abs(d - candidate) <= candidate * 0.10
                                     for d in intervals) / len(intervals)
                    if confidence >= 0.75:
                        period = candidate
        # timing and strength survive even when the rhythm is irregular.
        result.append(replace(hit, period=period, rhythm_confidence=confidence))
    return tuple(result)


def merge_maps(old: PulseMap | None, new: PulseMap) -> PulseMap:
    if old is None:
        return new
    if (old.hits is None) != (new.hits is None):
        return new
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
    events = [e for e in old.events if new.reference_at(e.media_time) is None]
    for a, b in spans:
        for t in (a, b):
            value = old.reference_at(t)
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
    hits = None
    if new.hits is not None:
        retained = []
        ratios = []
        new_times = [h.media_time for h in new.hits]
        for h in old.hits:
            if new.reference_at(h.media_time) is None:
                retained.append(h)
                continue
            # hit energy is raw capture rms, which scales with the app volume
            # at record time. beats both listens heard give the gain change
            # directly — dynamics cancel in the per-beat ratio.
            if not h.energy:
                continue
            index = bisect_left(new_times, h.media_time)
            near = [j for j in (index - 1, index) if 0 <= j < len(new_times)
                    and abs(new_times[j] - h.media_time) <= MATCH_WINDOW]
            if near:
                j = min(near, key=lambda j: abs(new_times[j] - h.media_time))
                if new.hits[j].energy:
                    ratios.append(h.energy / new.hits[j].energy)
        incoming = list(new.hits)
        if len(ratios) >= 5:
            # median over a handful of pairs so one clipped or missed beat
            # can't set the scale. no overlap → no evidence → energies stay
            # raw, same as a first listen.
            scale = statistics.median(ratios)
            incoming = [h if h.energy is None else replace(h, energy=h.energy * scale)
                        for h in incoming]
        hits = tuple(sorted(retained + incoming, key=lambda h: h.media_time))
        if len(hits) > MAX_HITS:
            return old
        hits = _rhythm_hints(hits, joined)
    return PulseMap(ordered, tuple(joined), hits)


class PulseRecorder:
    def __init__(self) -> None:
        self._segments: list[list[PulseSample]] = []
        self._segment: list[PulseSample] | None = None
        self._count = 0
        # checkpoint watermark: first segment / first sample not yet drained
        self._pending_segment = 0
        self._pending_offset = 0

    def gap(self) -> None:
        self._segment = None

    def record(self, position: float, level: float, kind: str = "kick", *,
               onset: float | None = None, energy: float | None = None, speed: float = 1.0) -> None:
        if (not math.isfinite(position) or position < 0
                or not math.isfinite(level) or not 0 <= level <= 1
                or not math.isfinite(speed) or speed <= 0
                or (onset is not None and (not math.isfinite(onset) or not 0 <= onset <= 1))
                or (energy is not None and (not math.isfinite(energy) or energy < 0))
                or kind not in KINDS or self._count >= MAX_EVENTS):
            self.gap()
            return
        if self._segment and position <= self._segment[-1].media_time:
            self.gap()
        if self._segment is None:
            self._segment = []
            self._segments.append(self._segment)
        self._segment.append(PulseSample(position, level, kind, onset, energy, speed))
        self._count += 1

    def segments(self) -> tuple[tuple[PulseSample, ...], ...]:
        return tuple(tuple(segment) for segment in self._segments if len(segment) >= 2)

    def snapshot(self) -> PulseMap | None:
        return map_from_segments(self.segments())

    def _quiet_cut(self, segment, start: int) -> int | None:
        # only cut where the hit extractor's state machine is idle and the
        # last attack has cleared the debounce, so no hit can be derived
        # half from one checkpoint and half from the next.
        cut = None
        last_loud = None
        for index in range(start, len(segment)):
            sample = segment[index]
            if (sample.onset or 0.0) > ONSET_LEAVE:
                last_loud = sample.media_time
            elif (last_loud is None
                  or (sample.media_time - last_loud) / sample.speed >= HIT_DEBOUNCE):
                cut = index
        return cut

    def drain_pending(self, *, final: bool = False):
        """Samples recorded since the last drain; advances the watermark.
        The previous cut sample leads each partial slice so checkpoint
        coverage spans share an endpoint and merge back into one. A live
        attack at the tail stays pending (unless ``final``) so its hit is
        derived from one contiguous stretch."""
        result = []
        index = self._pending_segment
        while index < len(self._segments):
            segment = self._segments[index]
            start = self._pending_offset if index == self._pending_segment else 0
            live = segment is self._segment and not final
            if live:
                cut = self._quiet_cut(segment, start)
                end = 0 if cut is None else cut + 1
            else:
                end = len(segment)
            if end > start:
                piece = tuple(segment[max(0, start - 1):end])
                if len(piece) >= 2:
                    result.append(piece)
            if live:
                self._pending_segment = index
                self._pending_offset = max(start, end)
                return tuple(result)
            index += 1
        self._pending_segment = len(self._segments)
        self._pending_offset = 0
        return tuple(result)

    def has_pending(self) -> bool:
        if self._pending_segment >= len(self._segments):
            return False
        if self._pending_segment < len(self._segments) - 1:
            return True
        return self._pending_offset < len(self._segments[self._pending_segment])

    def discard_tail(self, wall_seconds: float) -> None:
        """Drop the last ``wall_seconds`` of recording — capture that may
        already belong to the next track when a backend reports an external
        advance late. Gap durations are unknown and count as zero, so the
        trim errs long. Already-drained samples stay (a 30s checkpoint can
        race the advance; replay's disagree fallback covers that sliver)."""
        if not math.isfinite(wall_seconds) or wall_seconds <= 0:
            return
        remaining = float(wall_seconds)
        removed = False
        index = len(self._segments) - 1
        while remaining > 0 and index >= self._pending_segment:
            segment = self._segments[index]
            floor = self._pending_offset if index == self._pending_segment else 0
            if len(segment) > floor:
                speed = segment[-1].speed
                newest = segment[-1].media_time
                span = (newest - segment[floor].media_time) / speed
                threshold = newest - remaining * speed
                kept = len(segment)
                while kept > floor and segment[kept - 1].media_time > threshold:
                    kept -= 1
                if kept < len(segment):
                    self._count -= len(segment) - kept
                    del segment[kept:]
                    removed = True
                if kept > floor:
                    break
                remaining -= span
            index -= 1
        if removed:
            self.gap()


def _extract_hits(segment) -> tuple[PulseHit, ...]:
    hits = []
    peak = None
    last = 0.0
    peak_energy = None

    def finish():
        if peak is None:
            return
        hit = PulseHit(peak.media_time, peak.onset, peak.kind,
                       min(1.0, 0.3 + 0.7 * peak.onset), peak_energy)
        if hits and (hit.media_time - hits[-1].media_time) / peak.speed < HIT_DEBOUNCE:
            if hit.strength > hits[-1].strength:
                hits[-1] = hit
        else:
            hits.append(hit)

    for sample in segment:
        onset = sample.onset if sample.kind != "sustain" else 0.0
        renewed = (peak is not None and last < peak.onset * 0.45
                   and onset >= ONSET_ENTER and onset > last + ONSET_LEAVE)
        if peak is not None and (onset <= ONSET_LEAVE or renewed):
            finish()
            peak = None
            peak_energy = None
        if onset >= ONSET_ENTER and peak is None:
            peak = sample
        if peak is not None:
            if onset > peak.onset:
                peak = sample
            if sample.energy is not None:
                peak_energy = max(peak_energy or 0.0, sample.energy)
        last = onset
    finish()
    return tuple(hits)


def map_from_segments(segments) -> PulseMap | None:
    result = None
    for segment in segments:
        events = tuple(PulseEvent(s.media_time, s.strength, s.kind) for s in segment)
        coverage = ((segment[0].media_time, segment[-1].media_time),)
        detailed = all(getattr(s, "onset", None) is not None for s in segment)
        hits = _rhythm_hints(_extract_hits(segment), coverage) if detailed else None
        result = merge_maps(result, PulseMap(events, coverage, hits))
    return result


class PulseScheduler:
    def __init__(self, pulse_map: PulseMap | None = None) -> None:
        self.pulse_map = pulse_map
        self.disagreed = False
        self.gap()

    def value_at(self, position: float, speed: float = 1.0) -> float | None:
        if self.disagreed or self.pulse_map is None:
            return None
        return self.pulse_map.value_at(position, speed)

    def gap(self) -> None:
        self._bad_time = 0.0
        self._last_observation: float | None = None
        self._next_hit: int | None = None
        self._matched: set[int] = set()
        self._conflicts = 0
        self._live_start: float | None = None
        self._live_match: int | None = None
        self._live_reused = False
        self._live_peak_at = 0.0
        self._last_onset = 0.0
        self._peak_onset = 0.0

    def observe(self, position: float, level: float, speed: float = 1.0, *,
                onset: float | None = None) -> None:
        if self.disagreed:
            return
        if not all(math.isfinite(v) for v in (position, level, speed)) or speed <= 0:
            self.gap()
            return
        expected = self.pulse_map.reference_at(position) if self.pulse_map is not None else None
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
        if self.pulse_map.hits is not None:
            if onset is None or not math.isfinite(onset):
                self.gap()
                return
            # clean time retires isolated mistakes; matching beats must not
            # erase a persistent extra attack between every pair of beats.
            self._conflicts = max(0.0, self._conflicts - elapsed / 4.0)
            self._observe_hits(position, max(0.0, min(1.0, onset)), speed)
            # attack matching cannot see a map that stayed dark where the
            # speakers hold sustained bass — no attacks, no conflicts. level
            # comparison is unsafe across replay speeds (the recorded release
            # runs in record-time wall seconds), but recorded *silence* is
            # speed-proof once the look-back rejects a tail still descending.
            dark = (expected <= SILENCE_LEVEL
                    and level >= PULSE_SUSTAIN_FLOOR / 2)
            if dark:
                earlier = self.pulse_map.reference_at(position - SILENCE_LOOKBACK)
                dark = earlier is not None and earlier <= SILENCE_LEVEL
            if dark:
                self._bad_time += elapsed
            else:
                self._bad_time = max(0.0, self._bad_time - elapsed * 0.5)
            if self._bad_time >= 0.30:
                self.disagreed = True
            return
        if abs(expected - level) > 0.25:
            self._bad_time += elapsed
        else:
            self._bad_time = max(0.0, self._bad_time - elapsed * 0.5)
        if self._bad_time >= 0.30:
            self.disagreed = True

    def _observe_hits(self, position: float, onset: float, speed: float) -> None:
        times = self.pulse_map._hit_times
        tolerance = MATCH_WINDOW * speed
        if self._next_hit is None:
            self._next_hit = bisect_left(times, position)
        renewed = (self._live_start is not None and self._last_onset < self._peak_onset * 0.45
                   and onset >= ONSET_ENTER and onset > self._last_onset + ONSET_LEAVE)
        if self._live_start is not None and (onset <= ONSET_LEAVE or renewed):
            if self._live_match is None:
                self._conflicts += 1
            self._live_start = None
        if onset >= ONSET_ENTER and self._live_start is None:
            self._live_start = position
            self._live_match = None
            self._live_reused = False
            self._live_peak_at = position
            self._peak_onset = onset
        if self._live_start is not None:
            if onset > self._peak_onset:
                self._peak_onset = onset
                self._live_peak_at = position
                if (self._live_reused and self._live_match is not None
                        and abs(position - times[self._live_match]) / speed >= HIT_DEBOUNCE):
                    self._live_match = None
                    self._live_reused = False
            if self._live_match is None:
                start = bisect_left(times, position - tolerance)
                end = min(bisect_right(times, position + tolerance), start + 32)
                candidates = range(start, end)
                available = [i for i in candidates if i not in self._matched]
                if available:
                    self._live_match = min(available, key=lambda i: abs(times[i] - position))
                    self._matched.add(self._live_match)
                else:
                    # recording merges nearby peaks, even if their attack
                    # starts are farther apart. prefer distinct hits first.
                    nearby = [i for i in candidates if i in self._matched
                              and abs(times[i] - self._live_peak_at) / speed < HIT_DEBOUNCE]
                    if nearby:
                        self._live_match = min(nearby, key=lambda i: abs(times[i] - position))
                        self._live_reused = True
                # a slowly rising attack can peak long after it began.
                if (position - self._live_peak_at) / speed > 0.3 and self._live_match is None:
                    self._conflicts = 3
        while self._next_hit < len(times) and times[self._next_hit] < position - tolerance:
            if self._next_hit not in self._matched:
                self._conflicts += 1
            self._matched.discard(self._next_hit)
            self._next_hit += 1
        self._last_onset = onset
        if self._conflicts >= 3:
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
            # the drain owns the last listen's save; a bounded join here
            # silently dropped it whenever the final write ran long.
            self._thread.join()

    def _run(self) -> None:
        # pre-2.0 builds parked maps in the shared data store, which kept the
        # whole namespace resident; drop that file once.
        cache.clear_namespace(NAMESPACE)
        while (job := self._jobs.get()) is not None:
            operation, generation, key, pulse_map = job
            try:
                old = PulseMap.from_payload(cache.get_blob_json(NAMESPACE, key))
                if operation == "recording":
                    pulse_map = map_from_segments(pulse_map)
                if operation in ("save", "recording") and pulse_map is not None:
                    merged = merge_maps(old, pulse_map)
                    cache.put_blob_json(NAMESPACE, key, merged.payload(),
                                        TTL_SECONDS, MAX_PAYLOAD_BYTES)
                elif operation == "load" and not self._closed:
                    self.loaded.emit(generation, key, old)
            except Exception:
                if operation == "load" and not self._closed:
                    self.loaded.emit(generation, key, None)
