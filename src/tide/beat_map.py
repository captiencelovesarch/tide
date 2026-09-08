"""beat maps: the track's own audio, analyzed whole, driving the pulse.

the live detector judged every frame against the last two seconds of the
same song, so one kick scored differently depending on what came just
before it. this module instead decodes the stream tide is about to play
(ffmpeg, well ahead of realtime), scores the whole track at once, locks a
beat grid to its tempo, and hands the ambient controller a map it can
schedule against the player clock. same beat, same strength, every listen.

pure numpy + subprocess, no qt. beat_service.py owns the worker thread and
the cache; ui/ambient.py schedules the result.
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
import math
import os
import re
import shutil
import subprocess

import numpy as np


SR = 22_050
N_FFT = 1024                 # 46 ms window: sharp enough for a kick's attack
HOP = 256                    # 11.6 ms frames
FRAME_S = HOP / SR
MAX_SECONDS = 20 * 60.0      # long mixes are analyzed this far and no further
DECODE_BLOCK_S = 2.0
PARTIAL_FIRST_S = 10.0       # first partial map after this much audio

LOW_HZ = (35.0, 160.0)       # where a kick lives: strength and swell read here
FULL_HZ = (30.0, 8000.0)     # periodicity reads the whole mix
BPM_MIN, BPM_MAX = 30.0, 240.0
BPM_PREFER = (70.0, 160.0)   # below the low end a tempo may double
TEMPO_PRIOR_BPM = 120.0
TEMPO_PRIOR_OCTAVES = 1.0
LOCAL_WINDOW_S = 8.0         # local tempo is read over this much audio...
LOCAL_HOP_S = 2.0            # ...every this often
LOCAL_DRIFT = 0.25           # a local period may stray this far from global
TIGHTNESS = 60.0             # dp penalty on off-tempo beat spacing
LOW_WEIGHT = 0.5             # share of the tracking score read from the low band
CONFIDENCE_FLOOR = 0.3       # below this the grid is not trusted: hits only
LOW_REF_PERCENTILE = 92.0    # "a full kick" is this percentile of beat lows
LOW_REF_MIN = 6.0            # ...but never less than this absolute flux
LOW_SUM_FRAMES = 3           # a kick's rise is read over this many frames
OFFGRID_MIN = 0.9            # off-grid low peaks this strong survive as hits
OFFGRID_GAP = 0.3            # ...this many periods clear of the grid
OFFGRID_VS_BEAT = 0.9        # ...and at least this much of the nearest beat's low
OFFGRID_GAP_S = 0.09
SWELL_WINDOW_S = 1.5
SWELL_RATE = 4.0
HALF_TIME_PERIOD_S = 0.4     # above 150 bpm the pulse rides every other beat
# a rise that starts at t shows up in the frame whose window is just
# catching it at the trailing edge, so the frame center sits a little
# before the onset. measured on synthetic kicks (tools/pulse_lab.py
# --calibrate); tests/test_beat_map.py pins it.
ONSET_LEAD_S = 0.0086

# pulse shaping, in wall seconds
ATTACK_S = 0.048             # three render frames, peaking on the beat
HOLD_S = 0.02
RELEASE_FRACTION = 0.35      # of the pulse period...
RELEASE_MIN_S = 0.09         # ...clamped here
RELEASE_MAX_S = 0.26
HIT_RELEASE_S = 0.17
FLOOR_FROM_SWELL = 0.25
DEPTH_MIN = 0.5

MAX_BEATS = 40_000
MAX_HITS = 40_000
MAX_SWELL = 40_000
COVER_SLACK_S = 0.25

_SCHEME_RE = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.-]*)://")


def _bins(lo_hz: float, hi_hz: float) -> tuple[int, int]:
    hz = SR / N_FFT
    lo = max(1, int(round(lo_hz / hz)))
    hi = min(N_FFT // 2, int(round(hi_hz / hz))) + 1
    return lo, max(lo + 1, hi)


_LOW = _bins(*LOW_HZ)
_FULL = _bins(*FULL_HZ)
_WINDOW = np.hanning(N_FFT).astype(np.float32)
_LAG_MIN = int(round(60.0 / BPM_MAX / FRAME_S))
_LAG_MAX = int(round(60.0 / BPM_MIN / FRAME_S))


# ---------- the map ----------

@dataclass(frozen=True, slots=True)
class Beat:
    time: float
    strength: float
    phase: int        # 0 is the strongest of four; 0 and 2 carry half-time
    period: float     # local beat period in media seconds


@dataclass(frozen=True, slots=True)
class Hit:
    time: float
    strength: float


def _shape(t: float, strength: float, position: float, speed: float,
           release: float) -> float:
    age = (position - t) / speed
    if age < -ATTACK_S:
        return 0.0
    if age < 0.0:
        x = 1.0 + age / ATTACK_S
        return strength * x * x * (3.0 - 2.0 * x)
    if age <= HOLD_S:
        return strength
    return strength * math.exp(-(age - HOLD_S) / release)


@dataclass(frozen=True)
class BeatMap:
    duration: float
    complete: bool
    tempo: float
    confidence: float
    beats: tuple[Beat, ...]
    hits: tuple[Hit, ...]
    swell: tuple[float, ...]
    swell_rate: float = SWELL_RATE
    _beat_times: tuple[float, ...] = field(init=False, repr=False, compare=False)
    _hit_times: tuple[float, ...] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_beat_times", tuple(b.time for b in self.beats))
        object.__setattr__(self, "_hit_times", tuple(h.time for h in self.hits))

    def covers(self, position: float) -> bool:
        return (math.isfinite(position)
                and -COVER_SLACK_S <= position <= self.duration + COVER_SLACK_S)

    def swell_at(self, position: float) -> float:
        if not self.swell:
            return 0.0
        x = max(0.0, position * self.swell_rate)
        i = int(x)
        if i >= len(self.swell) - 1:
            return self.swell[-1]
        f = x - i
        return self.swell[i] + f * (self.swell[i + 1] - self.swell[i])

    def value_at(self, position: float, speed: float = 1.0) -> float | None:
        """the shaped pulse at a media position, or None outside coverage.

        floor and depth come from the swell (how loud this stretch is
        against the whole song); the beats ride on top. the attack starts
        early and peaks on the beat; the release is a fraction of the
        beat period so fast songs stay crisp and slow ones breathe."""
        if not math.isfinite(speed) or speed <= 0.0 or not self.covers(position):
            return None
        swell = self.swell_at(position)
        floor = FLOOR_FROM_SWELL * swell
        depth = DEPTH_MIN + (1.0 - DEPTH_MIN) * swell
        env = 0.0
        reach = (6.0 * RELEASE_MAX_S + HOLD_S) * speed
        lo = bisect_left(self._beat_times, position - reach)
        hi = bisect_right(self._beat_times, position + ATTACK_S * speed)
        for i in range(lo, hi):
            b = self.beats[i]
            if b.period < HALF_TIME_PERIOD_S:
                if b.phase % 2:
                    continue
                pulse_period = 2.0 * b.period
            else:
                pulse_period = b.period
            release = min(RELEASE_MAX_S, max(
                RELEASE_MIN_S, RELEASE_FRACTION * pulse_period / speed))
            env = max(env, _shape(b.time, b.strength, position, speed, release))
        lo = bisect_left(self._hit_times, position - reach)
        hi = bisect_right(self._hit_times, position + ATTACK_S * speed)
        for i in range(lo, hi):
            h = self.hits[i]
            env = max(env, _shape(h.time, h.strength, position, speed, HIT_RELEASE_S))
        return max(0.0, min(1.0, floor + (1.0 - floor) * depth * env))

    def payload(self) -> dict:
        return {
            "version": 1,
            "duration": round(self.duration, 4),
            "complete": bool(self.complete),
            "tempo": round(self.tempo, 3),
            "confidence": round(self.confidence, 4),
            "beats": [[round(b.time, 4), round(b.strength, 3), int(b.phase),
                       round(b.period, 4)] for b in self.beats],
            "hits": [[round(h.time, 4), round(h.strength, 3)] for h in self.hits],
            "swell_rate": self.swell_rate,
            "swell": [round(v, 3) for v in self.swell],
        }

    @classmethod
    def from_payload(cls, payload) -> BeatMap | None:
        try:
            if not isinstance(payload, dict) or payload.get("version") != 1:
                return None
            duration = float(payload["duration"])
            tempo = float(payload["tempo"])
            confidence = float(payload["confidence"])
            rate = float(payload["swell_rate"])
            if not all(math.isfinite(v) for v in (duration, tempo, confidence, rate)):
                return None
            if duration < 0 or tempo < 0 or not 0 <= confidence <= 1 or not 0.5 <= rate <= 60:
                return None
            raw_beats = payload["beats"]
            raw_hits = payload["hits"]
            raw_swell = payload["swell"]
            if (not isinstance(raw_beats, list) or not isinstance(raw_hits, list)
                    or not isinstance(raw_swell, list)
                    or len(raw_beats) > MAX_BEATS or len(raw_hits) > MAX_HITS
                    or len(raw_swell) > MAX_SWELL):
                return None
            beats = tuple(Beat(float(t), float(s), int(p), float(d))
                          for t, s, p, d in raw_beats)
            previous = -math.inf
            for b in beats:
                if (not all(math.isfinite(v) for v in (b.time, b.strength, b.period))
                        or b.time <= previous or not 0 <= b.strength <= 1
                        or b.phase not in (0, 1, 2, 3) or not 0.1 <= b.period <= 5.0):
                    return None
                previous = b.time
            hits = tuple(Hit(float(t), float(s)) for t, s in raw_hits)
            previous = -math.inf
            for h in hits:
                if (not math.isfinite(h.time) or not math.isfinite(h.strength)
                        or h.time <= previous or not 0 <= h.strength <= 1):
                    return None
                previous = h.time
            swell = tuple(float(v) for v in raw_swell)
            if any(not math.isfinite(v) or not 0 <= v <= 1 for v in swell):
                return None
            return cls(duration, bool(payload["complete"]), tempo, confidence,
                       beats, hits, swell, rate)
        except (KeyError, TypeError, ValueError, OverflowError):
            return None


# ---------- decoding ----------

def is_analyzable(target: str | None) -> bool:
    """http(s) urls and local files only, and only with ffmpeg on the box."""
    if not isinstance(target, str) or not target or shutil.which("ffmpeg") is None:
        return False
    m = _SCHEME_RE.match(target)
    if m is None:
        return os.path.isfile(target)
    scheme = m.group(1).lower()
    if scheme in ("http", "https"):
        return True
    if scheme == "file":
        return os.path.isfile(target[len("file://"):])
    return False


def decode_pcm(target: str, headers: dict | None = None, *,
               max_seconds: float = MAX_SECONDS,
               block_seconds: float = DECODE_BLOCK_S) -> Iterator[np.ndarray]:
    """mono float32 at SR, in blocks, straight off ffmpeg's stdout."""
    if not is_analyzable(target):
        return
    argv = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error"]
    if target.lower().startswith(("http://", "https://")):
        argv += ["-reconnect", "1", "-reconnect_streamed", "1",
                 "-reconnect_delay_max", "4"]
        if headers:
            argv += ["-headers", "".join(f"{k}: {v}\r\n" for k, v in headers.items())]
    argv += ["-i", target, "-vn", "-sn", "-dn", "-map", "0:a:0",
             "-ac", "1", "-ar", str(SR), "-f", "f32le",
             "-t", f"{max_seconds:.3f}", "pipe:1"]
    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    block = int(SR * block_seconds) * 4
    try:
        assert proc.stdout is not None
        while True:
            data = proc.stdout.read(block)
            if not data:
                break
            usable = len(data) - len(data) % 4
            if usable:
                yield np.frombuffer(data[:usable], dtype=np.float32)
    finally:
        try:
            proc.kill()
        except Exception:
            pass
        try:
            proc.wait(timeout=2.0)
        except Exception:
            pass


# ---------- analysis ----------

def _acf(segment: np.ndarray, lag_max: int) -> np.ndarray:
    x = segment - segment.mean()
    n = x.size
    size = 1 << (2 * n - 1).bit_length()
    f = np.fft.rfft(x, size)
    ac = np.fft.irfft(f * np.conj(f), size)[:lag_max + 1]
    if ac[0] <= 1e-12:
        return np.zeros(lag_max + 1)
    # fewer products go into longer lags; undo that bias before comparing
    ac *= n / np.maximum(1.0, n - np.arange(lag_max + 1))
    return ac / ac[0]


def _prefer_octave(lag: int, acf: np.ndarray) -> int:
    """a too-slow estimate may double if the faster lag is nearly as
    periodic. never halve: a fast grid is right for fast music, and the
    half-time pulse rule keeps the screen calm."""
    bpm = 60.0 / (lag * FRAME_S)
    if bpm >= BPM_PREFER[0]:
        return lag
    alt = lag // 2
    lo, hi = max(_LAG_MIN, alt - 2), min(_LAG_MAX, alt + 2)
    if lo > hi:
        return lag
    best = lo + int(np.argmax(acf[lo:hi + 1]))
    return best if acf[best] >= 0.5 * acf[lag] else lag


def _tempo(onset: np.ndarray) -> tuple[float, float, np.ndarray | None]:
    """global tempo (bpm), confidence 0..1, and a per-frame local period."""
    n = onset.size
    if n < 2 * _LAG_MAX + 2:
        return 0.0, 0.0, None
    win = min(n, int(round(LOCAL_WINDOW_S / FRAME_S)))
    hop = max(1, int(round(LOCAL_HOP_S / FRAME_S)))
    starts = list(range(0, max(1, n - win + 1), hop))
    if starts[-1] + win < n:
        starts.append(n - win)
    acfs = np.array([_acf(onset[s:s + win], _LAG_MAX) for s in starts])
    centers = np.array([s + win / 2.0 for s in starts])
    mean_acf = acfs.mean(axis=0)
    lags = np.arange(_LAG_MIN, _LAG_MAX + 1)
    bpm = 60.0 / (lags * FRAME_S)
    prior = np.exp(-0.5 * (np.log2(bpm / TEMPO_PRIOR_BPM) / TEMPO_PRIOR_OCTAVES) ** 2)
    best = int(lags[int(np.argmax(mean_acf[lags] * prior))])
    best = _prefer_octave(best, mean_acf)
    peak = float(mean_acf[best])
    confidence = max(0.0, min(1.0, (peak - 0.05) / 0.25))
    lo = max(_LAG_MIN, int(math.floor(best * (1.0 - LOCAL_DRIFT))))
    hi = min(_LAG_MAX, int(math.ceil(best * (1.0 + LOCAL_DRIFT))))
    local = []
    for a in acfs:
        seg = a[lo:hi + 1]
        j = int(np.argmax(seg))
        local.append(lo + j if seg[j] >= 0.5 * peak else best)
    local_arr = np.array(local, dtype=float)
    if local_arr.size >= 3:
        padded = np.concatenate(([local_arr[0]], local_arr, [local_arr[-1]]))
        local_arr = np.median(np.stack((padded[:-2], padded[1:-1], padded[2:])), axis=0)
    period = np.interp(np.arange(n, dtype=float), centers, local_arr)
    return 60.0 / (best * FRAME_S), confidence, period


def _smooth(x: np.ndarray, sigma: float = 1.5) -> np.ndarray:
    half = int(math.ceil(3 * sigma))
    k = np.exp(-0.5 * (np.arange(-half, half + 1) / sigma) ** 2)
    k /= k.sum()
    padded = np.pad(x, half, mode="edge")
    return np.convolve(padded, k, mode="valid")


def _track_beats(score: np.ndarray, period: np.ndarray) -> np.ndarray:
    """dynamic-programming beat tracking after ellis (2007): every frame
    asks which earlier frame, about one period back, would make the best
    previous beat, paying for spacing that strays from the local period."""
    n = score.size
    cum = np.zeros(n)
    back = np.full(n, -1, dtype=np.int64)
    for i in range(n):
        p = period[i]
        hi = i - int(round(0.5 * p))
        lo_raw = i - int(round(2.0 * p))
        best = -math.inf
        if lo_raw < 0:
            # a predecessor before time zero costs nothing but its spacing:
            # without this, frames in the first two periods are forced onto
            # a too-close real predecessor, the opening silence goes deeply
            # negative, and later beats prefer to leap back over it
            best = 0.0 if i + 1 <= p else -TIGHTNESS * math.log((i + 1) / p) ** 2
        if hi >= 0:
            lo = max(0, lo_raw)
            gaps = np.log((i - np.arange(lo, hi + 1)) / p)
            vals = cum[lo:hi + 1] - TIGHTNESS * gaps * gaps
            j = int(np.argmax(vals))
            if vals[j] >= best:
                best = float(vals[j])
                back[i] = lo + j
        cum[i] = score[i] + (best if math.isfinite(best) else 0.0)
    tail = int(round(period[-1])) + 1
    if n > tail:
        end = n - tail + int(np.argmax(cum[n - tail:]))
    else:
        end = int(np.argmax(cum))
    beats = []
    i = end
    while i >= 0:
        beats.append(i)
        i = back[i]
    return np.array(beats[::-1], dtype=np.int64)


def _peak_near(x: np.ndarray, idx: np.ndarray, reach: int) -> np.ndarray:
    out = np.empty(idx.size)
    n = x.size
    for k, i in enumerate(idx):
        out[k] = x[max(0, i - reach):min(n, i + reach + 1)].max()
    return out


def _refine(idx: np.ndarray, full: np.ndarray, reach: int = 2) -> np.ndarray:
    out = np.empty_like(idx)
    n = full.size
    for k, i in enumerate(idx):
        lo = max(0, i - reach)
        out[k] = lo + int(np.argmax(full[lo:min(n, i + reach + 1)]))
    out = np.unique(out)
    return out


def _offgrid(low: np.ndarray, ref: float, beat_times: np.ndarray,
             beat_lows: np.ndarray, period: np.ndarray | None,
             times: np.ndarray, sharp: np.ndarray) -> list[tuple[float, float]]:
    """kicks the grid missed: as strong as the song's full kick, well clear
    of the nearest beat, and about as loud as that beat. a busy bassline
    is none of those, so it rides the grid instead of doubling it."""
    if low.size < 3:
        return []
    inner = low[1:-1]
    cand = np.flatnonzero((inner > low[:-2]) & (inner >= low[2:])
                          & (inner >= OFFGRID_MIN * ref)) + 1
    hits: list[tuple[float, float]] = []
    n = low.size
    for i in cand:
        # the summed curve peaks a frame or two after the attack; time the
        # hit by the single-frame rise underneath it
        lo = max(0, i - LOW_SUM_FRAMES)
        t = float(times[lo + int(np.argmax(sharp[lo:min(n, i + 2)]))])
        gap = OFFGRID_GAP_S
        if period is not None:
            gap = max(gap, OFFGRID_GAP * float(period[i]) * FRAME_S)
        if beat_times.size:
            j = np.searchsorted(beat_times, t)
            ks = [k for k in (j - 1, j) if 0 <= k < beat_times.size]
            k = min(ks, key=lambda k: abs(t - beat_times[k]))
            if abs(t - beat_times[k]) <= gap or low[i] < OFFGRID_VS_BEAT * beat_lows[k]:
                continue
        strength = min(1.0, float(low[i]) / ref)
        if hits and t - hits[-1][0] < 0.08:
            if strength > hits[-1][1]:
                hits[-1] = (t, strength)
            continue
        hits.append((t, strength))
    return hits


def _swell(rms: np.ndarray, times: np.ndarray, duration: float) -> np.ndarray:
    w = int(round(SWELL_WINDOW_S / FRAME_S)) | 1
    k = np.hanning(w + 2)[1:-1]
    k /= k.sum()
    padded = np.pad(rms, w // 2, mode="edge")
    sm = np.convolve(padded, k, mode="valid")
    ref = float(np.percentile(sm, 98))
    sm = np.clip(sm / max(ref, 1e-9), 0.0, 1.0)
    count = int(duration * SWELL_RATE) + 2
    grid = np.arange(count) / SWELL_RATE
    return np.interp(grid, times, sm)


def _build(low: np.ndarray, full: np.ndarray, rms: np.ndarray,
           complete: bool) -> BeatMap:
    n = low.size
    duration = n * FRAME_S
    times = (np.arange(n) * HOP + N_FFT / 2.0) / SR + ONSET_LEAD_S
    ref_full = float(np.percentile(full, 98)) if n else 0.0
    onset = np.clip(full / max(ref_full, 1e-6), 0.0, 1.0)
    tempo, confidence, period = _tempo(onset)
    # a kick's attack spans a few frames and splits across their edges
    # differently each time; summing the rise over LOW_SUM_FRAMES reads it
    # the same wherever it lands in the frame grid
    low2 = low.copy()
    for k in range(1, LOW_SUM_FRAMES):
        low2[k:] += low[:-k]
    beats: tuple[Beat, ...] = ()
    beat_times = np.zeros(0)
    lows = np.zeros(0)
    if period is not None and confidence >= CONFIDENCE_FLOOR:
        # the grid should sit on the low-band events when it has a choice:
        # a snare is the louder onset, the kick is the one the pulse wants
        ref_low = float(np.percentile(low2, 98))
        low_norm = np.clip(low2 / max(ref_low, 1e-6), 0.0, 1.0)
        score = _smooth((1.0 - LOW_WEIGHT) * onset + LOW_WEIGHT * low_norm)
        idx = _refine(_track_beats(score, period), full)
        idx = idx[(idx >= 0) & (idx < n)]
        lows = _peak_near(low2, idx, 2)
        ref = max(LOW_REF_MIN, float(np.percentile(lows, LOW_REF_PERCENTILE))) if lows.size else LOW_REF_MIN
        strengths = np.clip(lows / ref, 0.0, 1.0)
        if idx.size >= 4:
            k = int(np.argmax([lows[j::4].mean() for j in range(4)]))
        else:
            k = 0
        beat_times = times[idx]
        beats = tuple(
            Beat(float(beat_times[j]), float(strengths[j]), int((j - k) % 4),
                 float(period[idx[j]] * FRAME_S))
            for j in range(idx.size))
    else:
        ref = max(LOW_REF_MIN, float(np.percentile(low2, 99))) if n else LOW_REF_MIN
    hits = tuple(Hit(t, s) for t, s in _offgrid(low2, ref, beat_times, lows, period, times, low))
    swell = tuple(float(v) for v in _swell(rms, times, duration)) if n else ()
    return BeatMap(duration, complete, tempo, confidence, beats, hits, swell)


class Analysis:
    """incremental spectra: push decoded audio as it arrives, build a map
    whenever. every build re-scores everything heard so far, so a partial
    map is exactly what the final one would say about that prefix."""

    def __init__(self) -> None:
        self._tail = np.zeros(0, dtype=np.float32)
        self._prev: np.ndarray | None = None
        self._prev_sq: np.ndarray | None = None
        self._low: list[np.ndarray] = []
        self._full: list[np.ndarray] = []
        self._rms: list[np.ndarray] = []
        self.frames = 0

    @property
    def seconds(self) -> float:
        return self.frames * FRAME_S

    def push(self, pcm: np.ndarray) -> None:
        pcm = np.nan_to_num(np.asarray(pcm, dtype=np.float32).ravel())
        buf = np.concatenate((self._tail, pcm)) if self._tail.size else pcm
        n = (buf.size - N_FFT) // HOP + 1 if buf.size >= N_FFT else 0
        if n <= 0:
            self._tail = buf
            return
        idx = np.arange(N_FFT)[None, :] + HOP * np.arange(n)[:, None]
        mag = np.abs(np.fft.rfft(buf[idx] * _WINDOW, axis=1))
        # full band: log flux, the usual onset measure, robust to dynamics.
        # low band: square-root flux, so a kick's energy outweighs the low
        # leakage of a snare or a strummed chord instead of being logged
        # down to nearly the same rise.
        logm = np.log1p(mag)
        band = mag[:, _LOW[0]:_LOW[1]]
        sq = np.sqrt(band)
        prev = self._prev if self._prev is not None else logm[:1]
        prev_sq = self._prev_sq if self._prev_sq is not None else sq[:1]
        rise = np.diff(np.vstack((prev, logm)), axis=0)
        np.maximum(rise, 0.0, out=rise)
        rise_sq = np.diff(np.vstack((prev_sq, sq)), axis=0)
        np.maximum(rise_sq, 0.0, out=rise_sq)
        self._low.append(rise_sq.sum(axis=1))
        self._full.append(rise[:, _FULL[0]:_FULL[1]].sum(axis=1))
        self._prev_sq = sq[-1:]
        self._rms.append(np.sqrt((band * band).sum(axis=1)) / (N_FFT / 4.0))
        self._prev = logm[-1:]
        self._tail = buf[n * HOP:]
        self.frames += n

    def build(self, complete: bool = False) -> BeatMap | None:
        if self.frames < 4:
            return None
        return _build(np.concatenate(self._low), np.concatenate(self._full),
                      np.concatenate(self._rms), complete)


def analyze(pcm: np.ndarray) -> BeatMap | None:
    analysis = Analysis()
    analysis.push(pcm)
    return analysis.build(True)


def analyze_target(target: str, headers: dict | None = None, *,
                   on_partial: Callable[[BeatMap], None] | None = None,
                   should_stop: Callable[[], bool] | None = None,
                   max_seconds: float = MAX_SECONDS) -> BeatMap | None:
    """decode and analyze a url or file. partial maps arrive after the
    first ten seconds and then at doubling intervals, so the frontier
    stays ahead of the playhead without re-scoring the track every block."""
    analysis = Analysis()
    next_at = PARTIAL_FIRST_S
    for block in decode_pcm(target, headers, max_seconds=max_seconds):
        if should_stop is not None and should_stop():
            return None
        analysis.push(block)
        if on_partial is not None and analysis.seconds >= next_at:
            partial = analysis.build(False)
            if partial is not None:
                on_partial(partial)
            next_at = max(next_at + PARTIAL_FIRST_S, analysis.seconds * 2.0)
    if should_stop is not None and should_stop():
        return None
    return analysis.build(True)
