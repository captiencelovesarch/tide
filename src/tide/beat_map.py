"""beat maps: the track's own audio, analyzed whole, driving the pulse.

the live detector judged every frame against the last two seconds of the
same song, so one kick scored differently depending on what came just
before it. this module instead decodes the stream tide is about to play
(ffmpeg, well ahead of realtime), scores the whole track at once, finds
its kicks, locks a beat grid to its tempo, and hands the ambient
controller a map it can schedule against the player clock. same beat,
same strength, every listen.

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

from . import kick_model


SR = 22_050
N_FFT = 1024                 # 46 ms window: sharp enough for a kick's attack
HOP = 256                    # 11.6 ms frames
FRAME_S = HOP / SR
MAX_SECONDS = 20 * 60.0      # long mixes are analyzed this far and no further
DECODE_BLOCK_S = 2.0
PARTIAL_FIRST_S = 10.0       # first partial map after this much audio

LOW_HZ = (35.0, 160.0)       # the tracker's low band: it pulls the grid toward kicks
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
LOW_SUM_FRAMES = 3           # a kick's rise is read over this many frames
ATTACK_BACK_S = 0.035        # a kick's attack may lead its low-band rise by this much
ATTACK_BACK = int(round(ATTACK_BACK_S / FRAME_S))
ATTACK_NEAR_TOP = 0.8
MERGE_S = 0.1                # kicks this close are one pulse (see _merge)
FIRST_SHARE = 0.5
# a beat sits on its kick, not on the grid: the grid says which beat, the
# kick says when. a kick within this reach of a grid beat moves it.
SNAP_FRACTION = 0.15         # of the local period...
SNAP_MIN_S = 0.035           # ...but never less than this

# ---------- kicks ----------
# the pulse follows the kicks, found first, with the grid only as a hint.
# measured against demucs drum stems on real songs (tools/pulse_bench), the
# old grid-first map showed 74% of kicks and fired 13 pulses a minute with no
# drum under them; this one shows 85% and fires 6.5.
#
# the kick signal: a wider frame (more low-frequency detail) split by median
# filtering into its sustained and percussive parts (fitzgerald 2010). a bass
# note is a horizontal line in the spectrogram and lands in the sustained
# part; a kick's pitch sweep is vertical. the percussive part's low-band rise
# finds the candidates.
WIDE_FFT = 2048
WIDE_TOP_HZ = 400.0          # the split only reads bins near the kick band
SPLIT_TIME = 17              # sustained: median over this many frames (~200 ms)
SPLIT_FREQ = 13              # percussive: median over this many bins (~140 hz)
KICK_HZ = (30.0, 180.0)
PEAK_REACH = 3               # a candidate tops the kick signal this many frames either side
KICK_REF_PERCENTILE = 95.0   # "a full kick" among the song's candidates
LOCAL_S = 6.0                # the local reference reads this far either side
# whether a candidate is a kick is learned: kick_model.py, a logistic
# regression over a log-mel patch at the candidate, trained by
# tools/pulse_bench against demucs drum stems. change anything that feeds
# _kick_rows (the bands, the patch, the candidate rules above) and retrain.
N_MEL = 32
MEL_HZ = (30.0, 10000.0)
PATCH_PRE = 2                # the patch's base frame sits this far before the candidate
PATCH_FRAMES = 5             # ...and this many frames are read against it
DECAY_FRAMES = (6, 12)       # the kick band's level 70 and 140 ms on, against its peak
# ...and a rule outside the model: a candidate whose kick band, 140 ms on,
# holds e^RING_MAX times more than the song's strongest hits do is a bass
# note ringing on, not a drum. an 808 song's strongest hits are 808s, so its
# 808s pass. on real songs this drops 0.3% of kicks.
STRONG_SHARE = 0.8           # "the strongest hits": candidates this near the full kick
RING_MAX = 2.0
KICK_SURE = 0.3              # model odds that pulse on a beat, eighth or sixteenth slot
OFF_SLOT_EXTRA = 0.2         # ...off every slot the model must be this much surer
# the hand rule: a candidate this strong against its local reference pulses
# too, unless the model says it is almost surely not a kick
KICK_VETO = 0.06
HAND_SLOT, HAND_FREE = 0.38, 0.6
# ...and a full-strength hit on a slot pulses whatever the model says: it
# can misread one odd patch (a hi-hat landing late inside it), and a drum
# this loud on the beat is not worth second-guessing
HAND_SURE = 0.85
LOCAL_SHARE = 0.85
LOCAL_FLOOR = 0.4            # the local reference never drops below this share of the song's
# no drums nearby: fewer than LONE_MIN sure kicks within LONE_S either
# side (a vocal, a drumless verse). there only a very sure kick pulses.
SURE_P = 0.5
LONE_S = 4.0
LONE_MIN = 3
LONE_P = 0.7
# slots: sixteenths of the local beat, or of half of it when the beat is
# slow (a half-time grid). a kick within the tolerance of one is on a slot.
SLOW_BEAT_S = 0.6
SLOT_TOL = 0.2               # of a slot...
SLOT_MIN_S = 0.03            # ...but never less than this
SWELL_WINDOW_S = 1.5
SWELL_RATE = 4.0
HALF_TIME_PERIOD_S = 0.4     # a beat faster than this releases over two beats' worth
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
MAX_LOUDNESS = 40_000
# the whole mix's level, unsmoothed, for drawing the song as a waveform
# (the seek bar). swell is the kick band, smoothed: it follows the drums.
LOUDNESS_RATE = 16.0         # values per media second
LOUDNESS_REF_PERCENTILE = 99.0
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
_WIDE_WINDOW = np.hanning(WIDE_FFT).astype(np.float32)


def _wide_bins(lo_hz: float, hi_hz: float) -> tuple[int, int]:
    hz = SR / WIDE_FFT
    return max(1, int(round(lo_hz / hz))), int(round(hi_hz / hz)) + 1


def _mel_bank() -> np.ndarray:
    """N_MEL triangles from MEL_HZ[0] to MEL_HZ[1] over the wide frame's bins."""
    def mel(f):
        return 2595.0 * np.log10(1.0 + f / 700.0)

    def hz(m):
        return 700.0 * (10.0 ** (m / 2595.0) - 1.0)
    edges = hz(np.linspace(mel(MEL_HZ[0]), mel(MEL_HZ[1]), N_MEL + 2))
    freqs = np.fft.rfftfreq(WIDE_FFT, 1.0 / SR)
    bank = np.zeros((N_MEL, freqs.size), np.float32)
    for k in range(N_MEL):
        lo, c, hi = edges[k], edges[k + 1], edges[k + 2]
        up = (freqs - lo) / max(c - lo, 1e-9)
        down = (hi - freqs) / max(hi - c, 1e-9)
        bank[k] = np.clip(np.minimum(up, down), 0.0, None)
        if bank[k].sum() == 0.0:
            bank[k, int(np.argmin(np.abs(freqs - c)))] = 1.0
    return bank


_KICK = _wide_bins(*KICK_HZ)
_WIDE_TOP = _wide_bins(0.0, WIDE_TOP_HZ)[1]
_MEL_FB = _mel_bank()
_KICK_WEIGHTS = np.asarray(kick_model.WEIGHTS, dtype=float)
_LAG_MIN = int(round(60.0 / BPM_MAX / FRAME_S))
_LAG_MAX = int(round(60.0 / BPM_MIN / FRAME_S))


# ---------- the map ----------

@dataclass(frozen=True, slots=True)
class Beat:
    time: float
    strength: float
    phase: int        # 0 is the strongest of four; 0 and 2 carry half-time
    period: float     # local beat period in media seconds
    pulse: bool = True  # false: no kick landed on this beat


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
    loudness: tuple[float, ...] = ()   # 0..1 full-band level, LOUDNESS_RATE per second
    loudness_rate: float = LOUDNESS_RATE
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
            if not b.pulse:
                continue
            pulse_period = 2.0 * b.period if b.period < HALF_TIME_PERIOD_S else b.period
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
            "version": 2,
            "duration": round(self.duration, 4),
            "complete": bool(self.complete),
            "tempo": round(self.tempo, 3),
            "confidence": round(self.confidence, 4),
            "beats": [[round(b.time, 4), round(b.strength, 3), int(b.phase),
                       round(b.period, 4), int(b.pulse)] for b in self.beats],
            "hits": [[round(h.time, 4), round(h.strength, 3)] for h in self.hits],
            "swell_rate": self.swell_rate,
            "swell": [round(v, 3) for v in self.swell],
            "loudness_rate": self.loudness_rate,
            "loudness": [round(v, 3) for v in self.loudness],
        }

    @classmethod
    def from_payload(cls, payload) -> BeatMap | None:
        try:
            if not isinstance(payload, dict) or payload.get("version") != 2:
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
            if any(len(r) != 5 or r[4] not in (0, 1) for r in raw_beats):
                return None
            beats = tuple(Beat(float(t), float(s), int(p), float(d), bool(u))
                          for t, s, p, d, u in raw_beats)
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
            # a map cached before loudness existed simply has none
            raw_loud = payload.get("loudness", [])
            loud_rate = float(payload.get("loudness_rate", LOUDNESS_RATE))
            if (not isinstance(raw_loud, list) or len(raw_loud) > MAX_LOUDNESS
                    or not math.isfinite(loud_rate) or not 1.0 <= loud_rate <= 120.0):
                return None
            loudness = tuple(float(v) for v in raw_loud)
            if any(not math.isfinite(v) or not 0 <= v <= 1 for v in loudness):
                return None
            if any(not math.isfinite(v) or not 0 <= v <= 1 for v in swell):
                return None
            return cls(duration, bool(payload["complete"]), tempo, confidence,
                       beats, hits, swell, rate, loudness, loud_rate)
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


def _refine(idx: np.ndarray, full: np.ndarray, reach: int = 2) -> np.ndarray:
    out = np.empty_like(idx)
    n = full.size
    for k, i in enumerate(idx):
        lo = max(0, i - reach)
        out[k] = lo + int(np.argmax(full[lo:min(n, i + reach + 1)]))
    out = np.unique(out)
    return out


def _onset_frame(low: np.ndarray, full: np.ndarray, i: int) -> int:
    """the summed curve peaks a frame or two after the attack; find the
    single-frame rise underneath it, then time the kick by its attack: the
    sharp full-band rise up to ATTACK_BACK_S before. a beater's click
    leads the body by a frame; an 808 that sweeps down from 300 hz is
    heard a frame or two before it reaches the low band."""
    lo = max(0, i - LOW_SUM_FRAMES)
    f = lo + int(np.argmax(low[lo:min(low.size, i + 2)]))
    back = max(0, f - ATTACK_BACK)
    window = full[back:f + 1]
    # the latest frame nearly as sharp as the sharpest: a hi-hat a few ms
    # ahead of the kick should not drag the pulse early
    near_top = np.flatnonzero(window >= ATTACK_NEAR_TOP * window.max())
    return back + int(near_top[-1])


def _sliding(x: np.ndarray, k: int, axis: int, fn) -> np.ndarray:
    """fn over a centred window of k along axis, edges held (like
    scipy.ndimage with mode="nearest"). chunked so a long song never
    builds the whole window stack at once."""
    half = k // 2
    out = np.empty_like(x)
    n = x.shape[0]
    step = 4096
    for s in range(0, n, step):
        e = min(n, s + step)
        if axis == 0:
            lo, hi = max(0, s - half), min(n, e + half)
            seg = np.pad(x[lo:hi], ((half - (s - lo), half - (hi - e)), (0, 0)), mode="edge")
        else:
            seg = np.pad(x[s:e], ((0, 0), (half, half)), mode="edge")
        win = np.lib.stride_tricks.sliding_window_view(seg, k, axis=axis)
        out[s:e] = fn(win, axis=-1)
    return out


def _kick_signal(wide: np.ndarray) -> np.ndarray:
    """per frame: the low-band rise of the percussive part, summed over
    LOW_SUM_FRAMES like the tracker's low band."""
    if wide.shape[0] == 0:
        return np.zeros(0)
    sustained = _sliding(wide, SPLIT_TIME, 0, np.median)
    percussive = _sliding(wide, SPLIT_FREQ, 1, np.median)
    mask = percussive * percussive / (percussive * percussive + sustained * sustained + 1e-12)
    lo, hi = _KICK
    sq = np.sqrt(wide[:, lo:hi] * mask[:, lo:hi])
    rise = np.zeros(wide.shape[0])
    rise[1:] = np.maximum(sq[1:] - sq[:-1], 0.0).sum(axis=1)
    out = rise.copy()
    for k in range(1, LOW_SUM_FRAMES):
        out[k:] += rise[:-k]
    return out


def _local_max(frames: np.ndarray, values: np.ndarray, reach: int) -> np.ndarray:
    """per entry: the largest value among the entries within reach frames."""
    out = np.empty(values.size)
    lo = np.searchsorted(frames, frames - reach, side="left")
    hi = np.searchsorted(frames, frames + reach, side="right")
    for k in range(values.size):
        out[k] = values[lo[k]:hi[k]].max()
    return out


def _kick_rows(logmel: np.ndarray, kick: np.ndarray, cand: np.ndarray,
               kick_ref: float, local: np.ndarray, body: np.ndarray) -> np.ndarray:
    """what the model reads at each candidate: the log-mel rise over a few
    frames against the frame before the attack, the level at the attack
    against the song's loud frames, the kick signal against the song's full
    kick and against its neighbours, and how far the kick band has fallen
    a little later."""
    ref = float(np.percentile(logmel.mean(axis=1), 95)) if logmel.size else 0.0
    base = logmel[cand - PATCH_PRE]
    rise = np.stack([logmel[cand - PATCH_PRE + 1 + k] - base for k in range(PATCH_FRAMES)], axis=1)
    level = logmel[cand] - ref
    v = kick[cand]
    peak = np.maximum.reduce([body[cand + k] for k in range(3)]) + 1e-6
    decay = np.stack([np.log((body[cand + k] + 1e-6) / peak) for k in DECAY_FRAMES], axis=1)
    return np.hstack((rise.reshape(cand.size, -1), level,
                      np.log(v / kick_ref + 1e-6)[:, None],
                      np.log(v / (local + 1e-9) + 1e-6)[:, None], decay))


def _kick_odds(rows: np.ndarray) -> np.ndarray:
    z = rows @ _KICK_WEIGHTS + kick_model.BIAS
    return 1.0 / (1.0 + np.exp(-np.clip(z, -40.0, 40.0)))


def _kick_candidates(wide: np.ndarray, mel: np.ndarray, n: int):
    """kick-signal peaks the model can judge: their frames, the kick
    signal, the song's full kick, the local references, the model's rows
    and how much longer each rings than the song's strongest hits."""
    kick = _kick_signal(wide)[:n]
    none = (np.zeros(0, np.int64), kick, 1.0, np.zeros(0), np.zeros((0, 0)), np.zeros(0))
    if kick.size < 2 * PEAK_REACH + 1:
        return none
    top = _sliding(kick[:, None], 2 * PEAK_REACH + 1, 0, np.max)[:, 0]
    cand = np.flatnonzero((kick >= top) & (kick > 0))
    if cand.size:
        cand = cand[np.concatenate(([True], np.diff(cand) > 1))]
    if cand.size == 0:
        return none
    vals = kick[cand]
    kick_ref = max(1e-9, float(np.percentile(vals[vals > 0.05 * vals.max()], KICK_REF_PERCENTILE)))
    local = _local_max(cand, vals, int(round(LOCAL_S / FRAME_S)))
    usable = (cand - PATCH_PRE >= 0) & (cand - PATCH_PRE + PATCH_FRAMES < n) & (cand + DECAY_FRAMES[-1] < n)
    cand, local = cand[usable], local[usable]
    logmel = np.log(1e-3 + mel[:n])
    body = wide[:n, _KICK[0]:_KICK[1]].sum(axis=1)
    rows = _kick_rows(logmel, kick, cand, kick_ref, local, body)
    late = rows[:, -1]
    strong = kick[cand] >= STRONG_SHARE * kick_ref
    ring = late - float(np.median(late[strong] if strong.any() else late)) if cand.size else late
    return cand, kick, kick_ref, local, rows, ring


def _slot_distance(t: float, beat_times: np.ndarray) -> tuple[float, float]:
    """distance from t to the nearest slot of the local grid and how far
    off a slot may be, both in seconds."""
    j = int(np.searchsorted(beat_times, t)) - 1
    if j < 0 or j + 1 >= beat_times.size:
        return 0.0, 1.0
    a, b = float(beat_times[j]), float(beat_times[j + 1])
    slot = (b - a) / (4 if b - a <= SLOW_BEAT_S else 8)
    x = (t - a) / slot
    return abs(x - round(x)) * slot, max(SLOT_MIN_S, SLOT_TOL * slot)


def _kicks(low: np.ndarray, full: np.ndarray, wide: np.ndarray, mel: np.ndarray,
           times: np.ndarray, beat_times: np.ndarray) -> list[tuple[int, float, float]]:
    """(frame, time, strength) of every kick, one per drum."""
    cand, kick, kick_ref, local, rows, ring = _kick_candidates(wide, mel, low.size)
    if cand.size == 0:
        return []
    odds = _kick_odds(rows)
    hand_ref = np.clip(local * LOCAL_SHARE, LOCAL_FLOOR * kick_ref, kick_ref)
    sure_t = times[cand[odds >= SURE_P]]
    found = []
    for i, p, ref, r in zip(cand, odds, hand_ref, ring):
        if r > RING_MAX:
            continue
        f = _onset_frame(low, full, int(i))
        t = float(times[f])
        on_slot = True
        if beat_times.size:
            d, tol = _slot_distance(t, beat_times)
            on_slot = d <= tol
        need = KICK_SURE if on_slot else min(0.95, KICK_SURE + OFF_SLOT_EXTRA)
        at = float(times[i])
        lone = int(np.searchsorted(sure_t, at + LONE_S, side="right")
                   - np.searchsorted(sure_t, at - LONE_S, side="left")) < LONE_MIN
        if lone:
            need = max(need, LONE_P)
        ok = p >= need
        if not ok and not lone:
            rel = kick[i] / ref
            ok = (p >= KICK_VETO and rel >= (HAND_SLOT if on_slot else HAND_FREE)
                  or on_slot and rel >= HAND_SURE)
        if ok:
            found.append((f, t, min(1.0, float(kick[i]) / kick_ref), float(p)))
    return _merge(sorted(found))


def _merge(found: list[tuple[int, float, float, float]]) -> list[tuple[int, float, float]]:
    """(frame, time, strength) per pulse from (frame, time, strength,
    odds) per kick, in time order. kicks within MERGE_S of a group's first
    are one pulse. it sits on the first when the model is sure of that
    one and it has FIRST_SHARE of the louder's strength (a double kick
    starts there), otherwise on the louder (a soft ghost before the real
    hit). groups are measured from their first kick, so a run of fast
    kicks gives one pulse per pair instead of sliding along the run."""
    kept: list[tuple[int, float, float]] = []
    first_sure = False
    start = -math.inf
    for f, t, s, p in found:
        if kept and t - start < MERGE_S:
            kf, kt, ks = kept[-1]
            if first_sure and ks >= FIRST_SHARE * s:
                kept[-1] = (kf, kt, max(s, ks))
            elif s > ks:
                kept[-1] = (f, t, s)
            continue
        start = t
        first_sure = p >= SURE_P
        kept.append((f, t, s))
    return kept


def _loudness(level: np.ndarray | None, times: np.ndarray, duration: float) -> np.ndarray:
    """the full mix's level per 1/LOUDNESS_RATE s window (the loudest frame
    in it, so a short hit still shows), against the song's own peak level."""
    count = int(duration * LOUDNESS_RATE) + 1
    if level is None or level.size == 0 or count <= 0:
        return np.zeros(0)
    slot = np.minimum((times * LOUDNESS_RATE).astype(np.int64), count - 1)
    slot = np.maximum(slot, 0)
    out = np.zeros(count)
    np.maximum.at(out, slot, level)
    ref = float(np.percentile(level, LOUDNESS_REF_PERCENTILE))
    return np.clip(out / max(ref, 1e-9), 0.0, 1.0)


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
           wide: np.ndarray, mel: np.ndarray, complete: bool,
           level: np.ndarray | None = None) -> BeatMap:
    n = low.size
    duration = n * FRAME_S
    times = (np.arange(n) * HOP + N_FFT / 2.0) / SR + ONSET_LEAD_S
    ref_full = float(np.percentile(full, 98)) if n else 0.0
    onset = np.clip(full / max(ref_full, 1e-6), 0.0, 1.0)
    tempo, confidence, period = _tempo(onset)
    grid = np.zeros(0, dtype=np.int64)
    if period is not None and confidence >= CONFIDENCE_FLOOR:
        # a kick's attack spans a few frames and splits across their edges
        # differently each time; summing the rise over LOW_SUM_FRAMES reads
        # it the same wherever it lands in the frame grid. the grid should
        # sit on the low-band events when it has a choice: a snare is the
        # louder onset, the kick is the one the pulse wants.
        low2 = low.copy()
        for k in range(1, LOW_SUM_FRAMES):
            low2[k:] += low[:-k]
        ref_low = float(np.percentile(low2, 98))
        low_norm = np.clip(low2 / max(ref_low, 1e-6), 0.0, 1.0)
        score = _smooth((1.0 - LOW_WEIGHT) * onset + LOW_WEIGHT * low_norm)
        grid = _refine(_track_beats(score, period), full)
        grid = grid[(grid >= 0) & (grid < n)]
    grid_times = times[grid]
    kicks = _kicks(low, full, wide, mel, times, grid_times)
    kick_times = np.array([t for _f, t, _s in kicks])
    used: set[int] = set()
    beats: list[Beat] = []
    if grid.size:
        # each beat moves onto the nearest kick within its reach; a beat
        # with none keeps its grid place and does not pulse
        periods = [float(period[i] * FRAME_S) for i in grid]
        beat_times = grid_times.copy()
        strengths = np.zeros(grid.size)
        for j, bt in enumerate(grid_times):
            reach = max(SNAP_MIN_S, SNAP_FRACTION * periods[j])
            k = int(np.searchsorted(kick_times, bt))
            best = None
            for kk in (k - 1, k):
                if (0 <= kk < kick_times.size and kk not in used
                        and abs(kick_times[kk] - bt) <= reach
                        and (best is None or abs(kick_times[kk] - bt) < abs(kick_times[best] - bt))):
                    best = kk
            if best is not None:
                used.add(best)
                beat_times[j] = kick_times[best]
                strengths[j] = kicks[best][2]
        # the bar starts on whichever beat of four carries the most kick
        first = int(np.argmax([strengths[j::4].mean() for j in range(4)])) if grid.size >= 4 else 0
        last = -math.inf
        for j in range(grid.size):
            t = max(float(beat_times[j]), last + 1e-3)
            last = t
            beats.append(Beat(t, float(strengths[j]), (j - first) % 4, periods[j],
                              bool(strengths[j] > 0.0)))
    hits = tuple(Hit(t, s) for k, (_f, t, s) in enumerate(kicks) if k not in used)
    swell = tuple(float(v) for v in _swell(rms, times, duration)) if n else ()
    loudness = tuple(float(v) for v in _loudness(level, times, duration)) if n else ()
    return BeatMap(duration, complete, tempo, confidence, tuple(beats), hits, swell,
                   loudness=loudness)


class Analysis:
    """incremental spectra: push decoded audio as it arrives, build a map
    whenever. every build re-scores everything heard so far, so a partial
    map is exactly what the final one would say about that prefix."""

    def __init__(self) -> None:
        self._tail = np.zeros(0, dtype=np.float32)
        # wide frames centre where the narrow ones do, so they start half
        # the difference in window length before the audio
        self._wide_tail = np.zeros(WIDE_FFT // 2 - N_FFT // 2, dtype=np.float32)
        self._wide_frames = 0
        self._prev: np.ndarray | None = None
        self._prev_sq: np.ndarray | None = None
        self._low: list[np.ndarray] = []
        self._level: list[np.ndarray] = []
        self._full: list[np.ndarray] = []
        self._rms: list[np.ndarray] = []
        self._wide: list[np.ndarray] = []
        self._mel: list[np.ndarray] = []
        self.frames = 0

    @property
    def seconds(self) -> float:
        return self.frames * FRAME_S

    def push(self, pcm: np.ndarray) -> None:
        pcm = np.nan_to_num(np.asarray(pcm, dtype=np.float32).ravel())
        self._push_wide(pcm)
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
        self._level.append(np.sqrt((mag * mag).sum(axis=1)) / (N_FFT / 4.0))
        self._prev = logm[-1:]
        self._tail = buf[n * HOP:]
        self.frames += n

    def _wide_spectra(self, buf: np.ndarray, n: int) -> tuple[np.ndarray, np.ndarray]:
        out_low = np.empty((n, _WIDE_TOP), np.float32)
        out_mel = np.empty((n, N_MEL), np.float32)
        for s in range(0, n, 1024):
            e = min(n, s + 1024)
            idx = np.arange(WIDE_FFT)[None, :] + HOP * np.arange(s, e)[:, None]
            mag = np.abs(np.fft.rfft(buf[idx] * _WIDE_WINDOW, axis=1)).astype(np.float32)
            out_low[s:e] = mag[:, :_WIDE_TOP]
            out_mel[s:e] = mag @ _MEL_FB.T
        return out_low, out_mel

    def _push_wide(self, pcm: np.ndarray) -> None:
        buf = np.concatenate((self._wide_tail, pcm))
        n = (buf.size - WIDE_FFT) // HOP + 1 if buf.size >= WIDE_FFT else 0
        if n <= 0:
            self._wide_tail = buf
            return
        low, mel = self._wide_spectra(buf, n)
        self._wide.append(low)
        self._mel.append(mel)
        self._wide_tail = buf[n * HOP:]
        self._wide_frames += n

    def _wide_now(self) -> tuple[np.ndarray, np.ndarray]:
        """the wide spectra for every narrow frame so far: the last few
        need audio past the frontier, read as silence until it arrives."""
        wide = list(self._wide)
        mel = list(self._mel)
        missing = self.frames - self._wide_frames
        if missing > 0:
            buf = np.concatenate((self._wide_tail, np.zeros(missing * HOP + WIDE_FFT, np.float32)))
            low, m = self._wide_spectra(buf, missing)
            wide.append(low)
            mel.append(m)
        if not wide:
            return np.zeros((0, _WIDE_TOP), np.float32), np.zeros((0, N_MEL), np.float32)
        return np.concatenate(wide)[:self.frames], np.concatenate(mel)[:self.frames]

    def build(self, complete: bool = False) -> BeatMap | None:
        if self.frames < 4:
            return None
        wide, mel = self._wide_now()
        return _build(np.concatenate(self._low), np.concatenate(self._full),
                      np.concatenate(self._rms), wide, mel, complete,
                      level=np.concatenate(self._level))


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
