"""PipeWire/PulseAudio capture of tide's own playback for the visualizer.

PortAudio (which sounddevice wraps) doesn't expose sink monitors on
PipeWire+PA reliably — it just sees the default *input* (typically the
mic). Instead we shell out to ``parec``, which is the canonical PA way
to capture playback audio. Works on PipeWire and classic PulseAudio
identically.

Scope: auto mode captures TIDE's audio, not the desktop's. A sink's
``.monitor`` carries everything the system plays — Discord, browsers,
games — and all of it used to drive the backdrop pulse. Auto resolution
finds tide's own sink input (mpv announces itself as ``tide``; child
backends register their pid) and records just that stream via
``parec --monitor-stream``. The default sink's monitor remains the
fallback while tide has no open stream, and an explicitly picked monitor
source in settings always wins.

The capture loop runs in a Python thread; FFT and band-binning run there
too so the GUI thread only sees ready-to-render arrays. Results are
delivered on the GUI thread via Qt signals.

Debugging the bass pulse: launch with TIDE_PULSE_TRACE=/path/to/trace.csv
and every analysis chunk (~43/s) appends one row of detector internals
while capture runs — signals, per-gate values, envelope, and reset markers
at stream gaps. Play the song that misbehaves, then read the CSV to see
which gate ate (or invented) each hit.
"""
from __future__ import annotations

import atexit
import json
import math
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field

import numpy as np
from PySide6.QtCore import QObject, Signal


SAMPLE_RATE = 44_100
CHUNK = 1024              # samples per FFT chunk; ~23ms at 44.1kHz
BANDS = 32
SMOOTH_ALPHA = 0.45
EPS = 1e-9

# Bass-pulse detector — drives the adaptive-background pulse. Kept separate
# from the visualizer's band smoothing.
#
# The first onset-gated detector judged every song against fixed absolute
# thresholds and multiplied all of its signals together — and the field
# report was "works on maybe 60% of songs". Both halves of that design were
# the problem. A loud sustained bed log-compresses the whole band's rise, so
# a fixed flux threshold that a dry kick clears by 10x is missed by the same
# kick over a bed; a dense mix never clears an absolute bass-share floor
# even though its kicks surge the share; and multiplying gates means one
# compressed term (measured: contrast 0.12 with every other gate wide open)
# silences a hit every other signal saw clearly.
#
# v2 keeps the narrow-band, onset-first philosophy but judges each signal
# against the song's own recent behavior, and lets any one confident
# rhythm signal carry the pulse:
#   * per-bin rectified spectral flux over an extended low band (43-260 Hz):
#     a kick's own bins rise no matter how loud the sustained bed next to
#     them is, where the old summed-band flux was compressed to nothing;
#   * adaptive normalization — flux is scored against ring-buffer
#     percentiles of the last ~2 s (p50 = this song's "nothing happening",
#     p95 = this song's "biggest recent rise"), so the detector
#     self-calibrates to any mix density, mastering level, or volume, with
#     absolute span floors so silence can't amplify numeric dust;
#   * a bass-share *surge* path — the kick-band's share of total power
#     jumping relative to its own baseline — which is what a kick looks
#     like inside a wall-of-sound mix where the absolute share never gets
#     near dominance (the absolute-share gate stays, as the other door);
#   * a broadband percussive fallback for songs that keep rhythm out of
#     the bass entirely: when the bass channel has produced no onsets for
#     a while AND the broadband flux is spiky (percussive, not pad-like),
#     hits there pulse at reduced strength — "beat", not "kick";
#   * the onset path is NOT multiplied by the floor/peak contrast anymore:
#     contrast only scales the sustained-bass breathing (still capped at
#     PULSE_SUSTAIN_FLOOR). An audible kick over a loud bed now reads full
#     strength instead of inheriting the bed's compression.
# The moving floor/peak followers, the instant-attack envelope, and the
# no-fabricated-onsets rules around stream gaps all carry over unchanged.
PULSE_LOW_HZ = 35.0
PULSE_HIGH_HZ = 130.0     # level/share band: where a kick's *body* lives
PULSE_FLUX_HIGH_HZ = 260.0   # flux band extends over the kick's punch too
PULSE_GAIN = 6.0          # pre-log gain applied to raw kick-band magnitude
PULSE_TOLERANCE = 0.18    # ignore this much of the local bass range
PULSE_MIN_SPAN = 0.6      # log-domain floor-to-peak range minimum
# Absolute quiet guard, lowered from 3.0/4.2: it now only has to reject the
# noise floor and barely-audible leakage, not stand in for calibration.
# Measured: kicks at amp 0.03 (~-30 dBFS, a low-listening-volume monitor
# level) sit at raw 3.2 and must pulse; amp 0.01 sits at 2.2 and must not.
PULSE_QUIET_FLOOR = 2.3
PULSE_QUIET_FULL = 3.0
PULSE_SUSTAIN_FLOOR = 0.22   # ceiling for sustained (onset-free) bass level
PULSE_FRACTION_FLOOR = 0.12  # kick-band power share below this: no absolute path
PULSE_FRACTION_FULL = 0.34   # ...above this the absolute-share door is open
PULSE_FLOOR_RISE_S = 1.2  # sustained bass becomes "normal" over a few sec
PULSE_FLOOR_FALL_S = 0.7
PULSE_PEAK_FALL_S = 1.4
PULSE_RELEASE_S = 0.22    # decay time constant; attack is instantaneous
# --- adaptive-normalization tuning ---
PULSE_STATS_S = 2.0       # ring-buffer horizon the percentiles see
PULSE_ONSET_LO = 0.25     # normalized kick flux: onset ramps in here...
PULSE_ONSET_HI = 0.85     # ...and is a full-strength hit here
# Per-bin log step below which a bin's frame-to-frame change is treated as
# jitter, not signal (soft-threshold shrinkage before the flux sums). Kills
# the ±image interference wobble of steady deep bass and most noise wiggle
# at the source; a real onset's log-unit bin steps barely notice it.
PULSE_BIN_DEADBAND = 0.25
# Span floors for (p95 - p50) in each normalizer, in each signal's own
# units. These are what keep near-silence and steady noise from amplifying
# their own dust to "the biggest thing this song has done lately".
PULSE_KSPAN_MIN = 1.2     # summed per-bin log flux, 43-260 Hz (~5 bins)
PULSE_FSPAN_MIN = 0.035   # bass-share surge (share is 0..1)
PULSE_BSPAN_MIN = 25.0    # summed per-bin log flux, 260 Hz-22 kHz (~505 bins)
# The surge path only counts when the surge lands on a real share — a jump
# from nothing to nothing-much is noise, not a kick...
PULSE_FSURGE_FLOOR = 0.06
PULSE_FSURGE_FULL = 0.16
PULSE_FSURGE_STRENGTH = 0.95
# ...and only in songs whose *resting* share is low. When bass already
# dominates the mix, share wiggle from the rest of the program modulating
# total power is meaningless — the energy-flux path owns those songs — so
# the surge door closes as the running share average rises.
PULSE_FRAC_AVG_S = 2.0
PULSE_SURGE_SHARE_LO = 0.30
PULSE_SURGE_SHARE_HI = 0.50
# --- broadband percussive fallback ---
PULSE_BB_STRENGTH = 0.62  # fallback pulses read "beat", never full "kick"
PULSE_BB_SPIK_LO = 1.6    # broadband flux spikiness (p95-p50)/(p50+eps):
PULSE_BB_SPIK_HI = 3.2    # steady noise/pads score ~0-1, percussion 3+
PULSE_BB_SPIK_EPS = 5.0
PULSE_BASS_ACT_LO = 0.03  # recent bass-onset activity below this: fallback
PULSE_BASS_ACT_HI = 0.10  # fully open; above this: fully closed
PULSE_BASS_ACT_FALL_S = 7.0   # how long the bass channel stays "recently active"

# Sustain integration — the answer to "constant bass turns tide into a
# flashbang". The envelope below is instant-attack / 0.22 s-release,
# which is exactly right for a sparse kick: it punches and clears. On a
# song with a bassline running underneath everything, though, onsets fire
# so densely that the envelope never finishes falling before it is
# re-triggered, and a hard re-trigger every ~300 ms is a strobe, not a
# pulse.
#
# So: measure how CONTINUOUSLY the pulse has been firing, and migrate the
# response along that axis. Sparse hits keep today's transient exactly
# (every term below is gated to a no-op at density 0, so a punchy song is
# bit-identical to before). As density rises the attack eases in, the
# release stretches, and a floor holds the light up between hits — the
# sustained bass reads as one long swell instead of a run of flashes.
#
# Density is measured as an ONSET RATE, not as a mean level: the level
# signal is a transient by construction (near zero between hits), so its
# average says more about tempo than about strobing. What actually makes
# the screen unpleasant is how many hard re-triggers per second the UI is
# asked to render. Measured on synthetic material:
#   kicks every 0.5 s ............ 1.9 hits/s   punchy, leave it alone
#   kicks every 0.5 s over a bed . 1.9 hits/s   still punchy
#   sustained bass, no kicks ..... 2.9 hits/s   the bed alone fires these
#   kicks every 0.25 s over a bed  3.8 hits/s   the flashbang
#   kicks every 0.17 s ........... 5.9 hits/s   solid wall
# so the band below keeps everything at or under ~2/s exactly as it is
# today and integrates hardest above ~3.5/s.
PULSE_DENSITY_HIT = 0.25     # hit_strength above this counts as an onset
PULSE_DENSITY_TAU_S = 2.0    # exponential-kernel window for the rate
# The raw accumulator sawtooths: it steps up on each edge and decays
# between, so at a rate sitting near the LO edge it crosses back and
# forth every beat and the integration weight chatters in time with the
# music. One more smoothing pass settles it to the mean. Asymmetric on
# purpose: committing to the swell should be deliberate, but when a song
# opens up — a breakdown after a drop — the punch has to come back
# promptly rather than staying smothered for another five seconds.
PULSE_DENSITY_SMOOTH_S = 1.0        # engaging
PULSE_DENSITY_SMOOTH_FALL_S = 0.45  # disengaging
# Calibrated too low on the first attempt: these were tuned against
# synthetic kicks, but real music with any bass under it sits above 3.6
# hits/sec essentially all the time, so the weight pegged at 1.0 within a
# second of playback and every song got the maximum treatment. The band
# is now wide and starts high, so ordinary material gets a partial shape
# and only relentless material reaches the full amount.
PULSE_DENSITY_LO = 3.5       # hits/sec — below this: untouched
PULSE_DENSITY_HI = 8.0       # hits/sec — above this: the full amount
# Deadband. smoothstep leaves a few thousandths of integration just under
# the LO edge; snapping that to zero is what makes "a sparse song behaves
# exactly as it did before" an exact claim rather than an approximate one.
PULSE_DENSITY_DEADBAND = 0.02
# The shaping itself, and the ONE knob that controls it: how much of the
# envelope's swing survives at full density. The pulse is compressed
# toward its own recent mean, which lifts the troughs and trims the peaks
# by the same proportion — the strobe is the CONTRAST between flash and
# background, so shrinking that contrast is the whole job, and the shape
# of every hit is preserved exactly.
#
# It is a floor, not a target: at 0.6 even a completely mis-calibrated
# density reading leaves 60% of the movement intact. That bound is
# deliberate. The first version stacked a softened attack, a stretched
# release and a rising floor, and when the density read high they
# combined into a near-flat line — the floor held the envelope above the
# incoming level, so hits stopped counting as attacks at all and the
# pulse starved itself. One bounded knob cannot do that.
PULSE_DEPTH_MIN = 0.6
PULSE_SUSTAIN_BED_S = 1.2    # window the compression centre is averaged over


def _build_band_edges(n_bands: int = BANDS, sample_rate: int = SAMPLE_RATE,
                       chunk: int = CHUNK, low_hz: float = 30.0,
                       high_hz: float = 16_000.0) -> np.ndarray:
    n_bins = chunk // 2 + 1
    hz_per_bin = (sample_rate / 2.0) / (n_bins - 1)
    log_lo = np.log10(low_hz)
    log_hi = np.log10(high_hz)
    cut_hz = np.logspace(log_lo, log_hi, n_bands + 1)
    edges = np.clip(np.round(cut_hz / hz_per_bin).astype(int), 1, n_bins - 1)
    for i in range(1, len(edges)):
        if edges[i] <= edges[i - 1]:
            edges[i] = min(n_bins - 1, edges[i - 1] + 1)
    return edges


_HANN_WINDOW = np.hanning(CHUNK).astype(np.float32)
_BAND_EDGES = _build_band_edges()


def _pulse_bin_range() -> tuple[int, int, int]:
    # At CHUNK=1024 a bin is ~43 Hz wide, so the kick band is only a few bins;
    # +1 keeps the bin nearest each top frequency inside the (exclusive) slice.
    hz_per_bin = (SAMPLE_RATE / 2.0) / (CHUNK // 2)
    lo = max(1, int(round(PULSE_LOW_HZ / hz_per_bin)))
    hi = max(lo + 1, int(round(PULSE_HIGH_HZ / hz_per_bin)) + 1)
    flux_hi = max(hi, int(round(PULSE_FLUX_HIGH_HZ / hz_per_bin)) + 1)
    return lo, hi, flux_hi


_PULSE_LO, _PULSE_HI, _PULSE_FLUX_HI = _pulse_bin_range()


# 192 synthetic decaying kicks (60/80/95 Hz, 32 chunk phases, amplitudes
# .03/.7): env >= .5 arrived 14.83 ms late at .7, 21.36-22.09 ms at .03.
# 15 ms covers the loud-hit median. parec's 10 ms is only a fallback request;
# a server snapshot replaces it when available. transport/display latency
# still needs a listening/loopback calibration.
_PULSE_CAPTURE_REQUEST_S = 0.010
_PULSE_ONSET_LATENCY_S = 0.015
_PULSE_LATENCY_ENV = "TIDE_PULSE_LATENCY_MS"
# latency-probe retries: the source output can lag the first audio chunk
_PULSE_PROBE_TRIES = 4
_PULSE_PROBE_RETRY_S = 1.0


@dataclass(frozen=True, slots=True)
class PulseFrame:
    captured_at: float
    level: float
    kind: str
    latency: float
    reset: bool = False
    scoped: bool = True
    # pre-envelope attack evidence; sustain and reset frames carry zero.
    onset: float | None = None
    # raw full-mix rms, before windowing; monitor volume still applies.
    energy: float | None = None


def _configured_pulse_latency() -> float | None:
    try:
        latency = float(os.environ[_PULSE_LATENCY_ENV]) / 1000.0
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    return latency if math.isfinite(latency) and latency >= 0.0 else None


def _pulse_kind(diag: dict | None) -> str:
    d = diag or {}
    sustain = d.get("contrast", 0.0) * PULSE_SUSTAIN_FLOOR
    onset = d.get("onset", 0.0)
    bass = d.get("gate_quiet", 0.0) * d.get("gate_fraction", 0.0)
    if d.get("level", 0.0) > bass * max(onset, sustain) + EPS:
        return "beat"
    if bass * onset > 0.0 and onset > sustain:
        return "kick"
    return "sustain"


def _pulse_onset(diag: dict | None, kind: str) -> float | None:
    if not diag:
        return None
    if kind == "beat":
        value = diag.get("level", 0.0)
    elif kind == "kick":
        value = (diag.get("gate_quiet", 0.0)
                 * diag.get("gate_fraction", 0.0) * diag.get("onset", 0.0))
    else:
        return 0.0
    return max(0.0, min(1.0, float(value))) if math.isfinite(value) else None


def _pulse_energy(samples: np.ndarray) -> float | None:
    if not samples.size:
        return None
    # float64 keeps squaring finite float32 input from overflowing.
    with np.errstate(over="ignore", invalid="ignore"):
        energy = math.sqrt(float(np.square(samples, dtype=np.float64).mean()))
    return energy if math.isfinite(energy) else None


# ---------- pulse trace (developer facility) ----------
#
# Set TIDE_PULSE_TRACE=/path/to/trace.csv before launching tide and every
# analysis chunk appends one row of detector internals (signals, gate values,
# final envelope) while capture runs. This is how "the backdrop missed the
# kicks in this song" turns into a diffable artifact instead of a vibe:
# play the failing song with the trace on, then read off which gate ate
# each hit. Costs one dict per chunk when off; rows flush about once a
# second when on, so a crash loses at most that much.

_TRACE_ENV = "TIDE_PULSE_TRACE"
_TRACE_COLUMNS = (
    "t", "reset", "raw", "floor", "peak", "span", "fraction", "frac_avg",
    "kflux", "fflux", "bflux", "knorm", "fnorm", "bnorm", "spik",
    "contrast", "gate_quiet", "onset", "gate_fraction", "act", "fallback_w",
    "level", "hrate", "hsm", "integ", "depth", "renv", "bed", "env",
)


class _PulseTracer:
    """Appends per-chunk detector diagnostics to a CSV. Never raises into
    the capture loop: any I/O failure closes the trace and capture carries
    on without it."""

    def __init__(self, path: str) -> None:
        self._dead = False
        self._fh = open(os.path.expanduser(path), "a", buffering=64 * 1024)
        if self._fh.tell() == 0:
            self._fh.write(",".join(_TRACE_COLUMNS) + "\n")
        self._last_flush = time.monotonic()

    def row(self, t: float, diag: dict | None, *, reset: bool = False) -> None:
        if self._dead:
            return
        try:
            vals = [f"{t:.4f}", "1" if reset else "0"]
            d = diag or {}
            for col in _TRACE_COLUMNS[2:]:
                v = d.get(col)
                vals.append("" if v is None else f"{v:.5g}")
            self._fh.write(",".join(vals) + "\n")
            now = time.monotonic()
            if now - self._last_flush >= 1.0:
                self._fh.flush()
                self._last_flush = now
        except Exception:
            self.close()

    def close(self) -> None:
        self._dead = True
        try:
            self._fh.flush()
            self._fh.close()
        except Exception:
            pass


# eq=False: the state carries numpy arrays, and a generated __eq__ would
# raise "truth value is ambiguous" on the first comparison. Identity is the
# only meaningful equality for a carried-forward state anyway.
@dataclass(eq=False)
class _PulseState:
    env: float
    floor: float
    peak: float
    raw: float = 0.0        # prev chunk's log kick-band energy
    fraction: float = 0.0   # prev chunk's kick-band power share, for the surge
    frac_avg: float = 0.0   # slow EMA of the share — the song's resting share
    # Each flux channel's own single-chunk rise, carried so an attack that
    # straddles a chunk boundary still sums to one onset. The two-chunk
    # window is exact: after a baseline reset the carries are zero and
    # nothing from before a gap can leak in.
    krise: float = 0.0      # kick-band (43-260 Hz) per-bin rectified log flux
    frise: float = 0.0      # bass-share rise
    brise: float = 0.0      # broadband (260 Hz up) per-bin rectified log flux
    # Prev chunk's full per-bin log magnitudes — what per-bin flux diffs
    # against. ~0.5 KB, recreated per chunk.
    lb: np.ndarray | None = None
    # Ring buffers of recent flux values (one slot per chunk, ~2 s each):
    # the percentile baselines that make every threshold per-song adaptive.
    # Allocated at anchor time and then shared (mutated in place) by every
    # subsequent state — only the newest state is ever alive.
    kbuf: np.ndarray | None = None
    fbuf: np.ndarray | None = None
    bbuf: np.ndarray | None = None
    nbuf: int = 0           # how much of the rings is filled
    ibuf: int = 0           # ring write index
    # Recent bass-onset activity (fast rise, ~7 s fall). While the bass
    # channel has been producing onsets, the broadband fallback stays shut.
    act: float = 0.0
    # Onset rate in hits/sec (exponential-kernel estimator) and the
    # rising-edge latch that feeds it. Drives the transient→swell
    # migration that keeps a constant bassline from strobing.
    hrate: float = 0.0
    hsm: float = 0.0        # hrate, smoothed — what the threshold reads
    hot: bool = False
    # The detector's own envelope, kept separate from the reported one.
    # ``env`` is the SHAPED output consumers read; ``renv`` is what the
    # attack/release iterates on. Splitting them is what guarantees the
    # shaping is a pure post-process: nothing it does can ever come back
    # round and change what the detector sees next chunk.
    renv: float = 0.0
    # Slow mean of the raw envelope — the centre the pulse is compressed
    # toward. Fed from ``renv``, never from the shaped output.
    bed: float = 0.0
    # The chunk's second half, carried so the next call can analyze the
    # 50%-overlap frame that straddles the boundary. Without it a kick
    # landing at a chunk edge is Hann-windowed to a whisper in BOTH
    # adjacent frames (measured: share 0.02 at the onset frame vs 0.2 for
    # a centered hit) — the classic reason onset analysis overlaps frames.
    tail: np.ndarray | None = None
    # Per-chunk diagnostics for the trace facility and the tests. Written
    # every chunk (a dict fill costs nothing at 43 Hz), never read back by
    # the detector itself.
    diag: dict | None = field(default=None, compare=False)


def _ema_alpha(dt: float, tau_s: float) -> float:
    return 1.0 - math.exp(-dt / max(0.001, tau_s))


def _smoothstep(edge0: float, edge1: float, value: float) -> float:
    if edge1 <= edge0:
        return 1.0 if value >= edge1 else 0.0
    x = max(0.0, min(1.0, (value - edge0) / (edge1 - edge0)))
    return x * x * (3.0 - 2.0 * x)


_PULSE_HOP = CHUNK // 2


def _compute_pulse(
    samples: np.ndarray, prev: _PulseState | None
) -> _PulseState | None:
    """Adaptive instant-attack / slow-release kick pulse in 0..1.

    Called once per CHUNK exactly as before, but internally analyzes two
    50%-overlapped Hann frames per call (the boundary-straddling frame
    first, then the chunk itself). Overlap keeps every transient near a
    frame center; without it a kick landing at a chunk edge is windowed
    down to a whisper in both adjacent frames and its bass share reads as
    noise. The returned state's ``env`` is the post-second-frame envelope;
    ``diag`` reports the louder of the two frames plus the final envelope.
    """
    dt = _PULSE_HOP / SAMPLE_RATE
    state = prev
    straddle_diag: dict | None = None
    tail = prev.tail if prev is not None else None
    if tail is not None and tail.shape[0] == _PULSE_HOP:
        frame = np.concatenate((tail, samples[:_PULSE_HOP]))
        state = _pulse_frame(frame, state, dt)
        if state is None:
            return None
        straddle_diag = state.diag
    state = _pulse_frame(samples, state, dt)
    if state is None:
        return None
    if (
        straddle_diag is not None
        and state.diag is not None
        and straddle_diag["level"] > state.diag["level"]
    ):
        straddle_diag["env"] = state.env
        state.diag = straddle_diag
    state.tail = samples[_PULSE_HOP:].copy()
    return state


def _pulse_frame(
    samples: np.ndarray, prev: _PulseState | None, dt: float
) -> _PulseState | None:
    """One analysis frame of the pulse detector.

    Onset-first and self-calibrating: per-bin spectral flux in the low
    band, a bass-share surge, and (when the bass has been quiet a while) a
    broadband percussive fallback are each scored against ring-buffer
    percentiles of the song's own last ~2 s, so "a hit" means "unusual for
    this song right now", not "past a constant someone tuned on one mix".
    The moving floor/peak contrast survives only as the throttle on
    sustained-bass breathing (capped at PULSE_SUSTAIN_FLOOR); it no longer
    multiplies the onset path. See the PULSE_* block for the full design.
    """
    windowed = samples * _HANN_WINDOW
    mag = np.abs(np.fft.rfft(windowed))
    power = mag * mag
    kick = float(mag[_PULSE_LO:_PULSE_HI].mean())
    raw = math.log1p(kick * PULSE_GAIN)
    # Kick-band share of the full spectrum (DC excluded). A real kick hit is
    # bass-dominated in the moment — or at least *surges toward* dominance;
    # loud vocals/snares/noise are neither.
    kick_power = float(power[_PULSE_LO:_PULSE_HI].sum())
    total_power = float(power[1:].sum())
    fraction = kick_power / max(EPS, total_power)

    if not (
        math.isfinite(raw)
        and math.isfinite(fraction)
        and math.isfinite(total_power)
    ):
        # One poisoned chunk (a client writing NaN into the monitored sink)
        # must not stick NaN into the followers or the flux ring buffers —
        # every NaN comparison is False, so they would never recover. The
        # total_power term matters here: it goes non-finite if ANY bin is,
        # which is what certifies the per-bin log magnitudes below as clean
        # before they can poison the rings. Drop the baseline instead: the
        # caller treats None as "start over", and the next clean chunk
        # re-anchors through the anchor branch, which computes no flux, so
        # the gap cannot fabricate an onset either.
        return None

    # Per-bin log magnitudes — what the flux channels diff against. Per-bin
    # (rather than flux-of-the-band-sum) is the fix for kicks over loud
    # sustained beds: the bed parks in its own bins and contributes zero
    # rise there, while the kick's bins rise by their full log step.
    lb = np.log1p(mag * PULSE_GAIN)

    if prev is None or prev.lb is None:
        # Fresh baseline — capture start, stale-audio drop, parec respawn,
        # or recovery from a NaN chunk. With no history there is no flux, so
        # a kick landing in this exact chunk is capped at the sustain floor.
        # That softened first hit is the deliberate cost of never fabricating
        # onsets at gap edges: "quiet before the gap, loud after" must not
        # read as a kick that never happened.
        floor = raw * 0.65
        peak = max(raw, floor + 0.55)
        env = 0.0
        krise = frise = brise = 0.0
        kflux = fflux = bflux = 0.0
        ring = max(8, int(round(PULSE_STATS_S / dt)))
        kbuf = np.zeros(ring)
        fbuf = np.zeros(ring)
        bbuf = np.zeros(ring)
        nbuf = 0
        ibuf = 0
        act = 0.0
        hrate = 0.0
        hsm = 0.0
        hot = False
        renv = 0.0
        bed = 0.0
        frac_avg = fraction
    else:
        floor_tau = PULSE_FLOOR_RISE_S if raw > prev.floor else PULSE_FLOOR_FALL_S
        floor_a = _ema_alpha(dt, floor_tau)
        floor = prev.floor + (raw - prev.floor) * floor_a

        if raw >= prev.peak:
            peak = raw
        else:
            peak_a = _ema_alpha(dt, PULSE_PEAK_FALL_S)
            peak = prev.peak + (floor - prev.peak) * peak_a
            peak = max(peak, raw)
        # env is derived fresh from renv every chunk (see the shaping
        # stage), so there is nothing to carry here — only renv.
        # A kick's attack can split across two frames, so neither frame's
        # rise clears the onset scoring on its own. Sum this frame's rise
        # with the previous frame's; a split onset then reads as the single
        # rise it is. Each state stores only its own rise, so the window is
        # exactly two frames — after a baseline reset the carry is zero and
        # nothing from before the gap can leak in.
        #
        # Per-bin soft deadband, then signed net (rises minus falls,
        # clamped at zero) — NOT rectified rises. Two measured reasons:
        # down in the low bins a steady deep sine is not per-bin steady
        # (its ±frequency images interfere, neighboring bins trade
        # magnitude in a slow beat, and rectifying counted every trade as
        # an onset — a pure 50 Hz sine held "flux" ~0.9 forever); and
        # broadband noise wiggles every bin a little each frame. The
        # deadband shrinks each bin's step toward zero by a fixed log
        # amount, which erases micro-jitter entirely while barely denting
        # a real onset's log-unit steps; the signed sum then cancels
        # whatever oscillation survives. The per-bin *log* step before the
        # sum is what keeps a kick over a loud bed uncompressed: the bed's
        # bins contribute ~zero while the kick's own quiet-before bins
        # rise by their full log step.
        dlb = lb - prev.lb
        dlb = (np.clip(dlb - PULSE_BIN_DEADBAND, 0.0, None)
               + np.clip(dlb + PULSE_BIN_DEADBAND, None, 0.0))
        krise = max(0.0, float(dlb[_PULSE_LO:_PULSE_FLUX_HI].sum()))
        brise = max(0.0, float(dlb[_PULSE_FLUX_HI:].sum()))
        frise = max(0.0, fraction - prev.fraction)
        kflux = krise + prev.krise
        fflux = frise + prev.frise
        bflux = brise + prev.brise
        kbuf, fbuf, bbuf = prev.kbuf, prev.fbuf, prev.bbuf
        ibuf = prev.ibuf
        nbuf = prev.nbuf
        act = prev.act
        hrate = prev.hrate
        hsm = prev.hsm
        hot = prev.hot
        renv = prev.renv
        bed = prev.bed
        frac_avg = prev.frac_avg + (
            fraction - prev.frac_avg) * _ema_alpha(dt, PULSE_FRAC_AVG_S)
        # Push before scoring: a lone hit then sits at its buffer's p95 and
        # scores ~1 against itself, which is exactly the self-scaling the
        # normalizers are for.
        ring = kbuf.shape[0]
        kbuf[ibuf] = kflux
        fbuf[ibuf] = fflux
        bbuf[ibuf] = bflux
        ibuf = (ibuf + 1) % ring
        nbuf = min(nbuf + 1, ring)

    # Adaptive scoring: p50 is this song's "nothing happening", p95 its
    # "biggest recent rise". The span floors keep silence and steady noise
    # from promoting their own dust; the clip means anything at or past the
    # recent-best rise is simply a full hit.
    if nbuf >= 4:
        # References ride p98, not p95: hits at a plain 0.5 s period
        # occupy ~4-5% of the ring's frames, so p95 lands right ON the
        # hit boundary and flickers between "reference = a hit" (between-
        # hit wiggle scores ~0) and "reference = baseline" (wiggle scores
        # as onsets). p98 (≈ third-largest of the ring) sits inside the
        # hits whenever the song has any, while still ignoring a lone
        # glitch frame.
        k50, k98 = np.percentile(kbuf[:nbuf], (50.0, 98.0))
        f50, f98 = np.percentile(fbuf[:nbuf], (50.0, 98.0))
        b50, b98 = np.percentile(bbuf[:nbuf], (50.0, 98.0))
        knorm = max(0.0, min(1.0, (kflux - k50) / max(PULSE_KSPAN_MIN, k98 - k50)))
        fnorm = max(0.0, min(1.0, (fflux - f50) / max(PULSE_FSPAN_MIN, f98 - f50)))
        bnorm = max(0.0, min(1.0, (bflux - b50) / max(PULSE_BSPAN_MIN, b98 - b50)))
        # Spikiness of the broadband flux distribution: percussion spends
        # most chunks near zero and a few very high (huge p98/p50 ratio);
        # steady noise and slow swells keep the two close together.
        spik = (b98 - b50) / (b50 + PULSE_BB_SPIK_EPS)
    else:
        knorm = fnorm = bnorm = spik = 0.0

    span = max(PULSE_MIN_SPAN, peak - floor)
    threshold = floor + span * PULSE_TOLERANCE
    contrast = (raw - threshold) / max(EPS, span * (1.0 - PULSE_TOLERANCE))
    contrast = max(0.0, min(1.0, contrast))
    # Absolute quiet guard only — calibration is the normalizers' job now.
    gate_quiet = _smoothstep(PULSE_QUIET_FLOOR, PULSE_QUIET_FULL, raw)
    # Two onset doors. The kick-flux path is the main one; the surge path
    # catches kicks whose *energy* rise is compressed (wall-of-sound, or a
    # slammed master where the hit is bass momentarily taking over the
    # spectrum, not the spectrum getting louder) — but a surge only counts
    # when it lands on a real share.
    surge_room = 1.0 - _smoothstep(
        PULSE_SURGE_SHARE_LO, PULSE_SURGE_SHARE_HI, frac_avg)
    surge = (fnorm * surge_room
             * _smoothstep(PULSE_FSURGE_FLOOR, PULSE_FSURGE_FULL, fraction))
    onset = max(
        _smoothstep(PULSE_ONSET_LO, PULSE_ONSET_HI, knorm),
        PULSE_FSURGE_STRENGTH * surge,
    )
    # The share gate has the same two doors: absolute dominance, or a real
    # surge. Broadband noise fails both — its share is a static sliver.
    gate_fraction = max(
        _smoothstep(PULSE_FRACTION_FLOOR, PULSE_FRACTION_FULL, fraction),
        PULSE_FSURGE_STRENGTH * surge,
    )
    # Onset carries the hit at full strength on its own; contrast only
    # throttles the sustained breathing. This decoupling is what lets an
    # audible kick over a loud bed read full instead of inheriting the
    # bed's log compression (measured 0.12 contrast with every gate open).
    level = gate_quiet * gate_fraction * max(
        onset, contrast * PULSE_SUSTAIN_FLOOR)

    # Recent bass-onset activity: rises with the hit just computed, falls
    # over ~7 s. While the bass channel is doing its job, the broadband
    # fallback stays shut so snares layered over kicks can't double-fire.
    hit_strength = gate_quiet * gate_fraction * onset
    if hit_strength > act:
        act = act + (hit_strength - act) * 0.5
    else:
        act = act + (hit_strength - act) * _ema_alpha(dt, PULSE_BASS_ACT_FALL_S)
    # The fallback: rhythm that lives entirely outside the bass. Requires a
    # percussive (spiky) broadband channel AND a bass channel that has been
    # quiet a while; pulses at reduced strength so it reads "beat", not
    # "kick". The quiet guard reuses overall loudness so a barely-audible
    # source stays dark here too.
    fallback_w = (
        (1.0 - _smoothstep(PULSE_BASS_ACT_LO, PULSE_BASS_ACT_HI, act))
        * _smoothstep(PULSE_BB_SPIK_LO, PULSE_BB_SPIK_HI, spik)
    )
    if fallback_w > 0.0:
        # Loudness by high-percentile bin, not mean: a percussive burst
        # lives in ~90 of 505 bins, and a mean would dilute clearly-audible
        # hits below the quiet guard. p90 asks "are the hot bins hot".
        raw_bb = math.log1p(float(np.percentile(mag[1:], 90.0)) * PULSE_GAIN)
        gate_quiet_bb = _smoothstep(PULSE_QUIET_FLOOR, PULSE_QUIET_FULL, raw_bb)
        level = max(level, PULSE_BB_STRENGTH * bnorm * fallback_w * gate_quiet_bb)

    # Onset rate, as an exponential-kernel estimator: each rising edge
    # deposits 1/tau and the accumulator decays at tau, so a steady stream
    # of f hits/sec settles at exactly f. Edge-triggered rather than
    # level-counted because one hit spans two or three chunks and would
    # otherwise be counted several times.
    now_hot = hit_strength > PULSE_DENSITY_HIT
    hrate *= math.exp(-dt / PULSE_DENSITY_TAU_S)
    if now_hot and not hot:
        hrate += 1.0 / PULSE_DENSITY_TAU_S
    hot = now_hot
    hsm_tau = (PULSE_DENSITY_SMOOTH_S if hrate > hsm
               else PULSE_DENSITY_SMOOTH_FALL_S)
    hsm = hsm + (hrate - hsm) * _ema_alpha(dt, hsm_tau)
    integ = _smoothstep(PULSE_DENSITY_LO, PULSE_DENSITY_HI, hsm)
    if integ < PULSE_DENSITY_DEADBAND:
        integ = 0.0

    # The detector envelope: instant attack, PULSE_RELEASE_S decay, exactly
    # as it has always been. Nothing below writes back into it.
    if level >= renv:
        renv = level
    else:
        decay = math.exp(-dt / PULSE_RELEASE_S)
        renv = renv * decay + level * (1.0 - decay)

    # ---- presentation shaping ----
    # Compress the pulse toward its own recent mean. Troughs lift and
    # peaks trim by the same proportion, so what shrinks is the CONTRAST
    # between flash and background — which is what reads as a strobe —
    # while the attack stays instant and the shape of every hit is
    # untouched. A pure function of (renv, bed, integ): the output is
    # never an input, so it cannot starve the attack or ratchet a floor.
    bed = bed + (renv - bed) * _ema_alpha(dt, PULSE_SUSTAIN_BED_S)
    if integ <= 0.0:
        # Short-circuit rather than multiply by a depth of 1.0:
        # ``bed + (renv - bed)`` is not bit-exactly ``renv`` in floating
        # point, and "a sparse song is untouched" should mean untouched.
        depth = 1.0
        env = renv
    else:
        depth = 1.0 - (1.0 - PULSE_DEPTH_MIN) * integ
        env = bed + (renv - bed) * depth
    return _PulseState(
        env=env, floor=floor, peak=peak, raw=raw, fraction=fraction,
        frac_avg=frac_avg, krise=krise, frise=frise, brise=brise, lb=lb,
        kbuf=kbuf, fbuf=fbuf, bbuf=bbuf, nbuf=nbuf, ibuf=ibuf, act=act,
        hrate=hrate, hsm=hsm, hot=hot, renv=renv, bed=bed,
        diag={
            "raw": raw, "floor": floor, "peak": peak, "span": span,
            "fraction": fraction, "frac_avg": frac_avg,
            "kflux": kflux, "fflux": fflux,
            "bflux": bflux, "knorm": knorm, "fnorm": fnorm, "bnorm": bnorm,
            "spik": spik, "contrast": contrast, "gate_quiet": gate_quiet,
            "onset": onset, "gate_fraction": gate_fraction,
            "act": act, "fallback_w": fallback_w, "level": level, "env": env,
            "hrate": hrate, "hsm": hsm, "integ": integ,
            "depth": depth, "renv": renv, "bed": bed,
        },
    )


def _compute_bands(samples: np.ndarray, prev: np.ndarray | None = None) -> np.ndarray:
    windowed = samples * _HANN_WINDOW
    spec = np.fft.rfft(windowed)
    mag = np.abs(spec)
    bands = np.empty(BANDS, dtype=np.float32)
    for i in range(BANDS):
        lo, hi = _BAND_EDGES[i], _BAND_EDGES[i + 1]
        if hi > lo:
            bands[i] = mag[lo:hi].mean()
        else:
            bands[i] = mag[lo]
    bands = np.log1p(bands * 8.0)
    ref = max(bands.max(), 1.5)
    bands = np.clip(bands / ref, 0.0, 1.0)
    if prev is not None and prev.shape == bands.shape:
        attack = bands > prev
        result = prev.copy()
        result[attack] = SMOOTH_ALPHA * bands[attack] + (1 - SMOOTH_ALPHA) * prev[attack]
        rel_a = SMOOTH_ALPHA * 0.45
        result[~attack] = rel_a * bands[~attack] + (1 - rel_a) * prev[~attack]
        return result
    return bands


def list_monitor_sources() -> list[tuple[str, str]]:
    """Return [(name, human_label), ...] for all available .monitor sources.

    Used by the settings dialog + viz cog so users can pick a non-default sink.
    """
    if shutil.which("pactl") is None:
        return []
    try:
        out = subprocess.run(
            ["pactl", "list", "sources", "short"],
            capture_output=True, text=True, timeout=2,
        )
    except Exception:
        return []
    result: list[tuple[str, str]] = []
    for line in out.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        name = parts[1]
        if not name.endswith(".monitor"):
            continue
        # Tidy up the label: drop the alsa_output prefix + .monitor suffix
        label = name
        if label.startswith("alsa_output."):
            label = label[len("alsa_output."):]
        if label.endswith(".monitor"):
            label = label[:-len(".monitor")]
        result.append((name, label))
    return result


# ---------- own-stream resolution ----------
#
# The feed should hear TIDE, not the desktop. A sink monitor carries
# everything the system plays — Discord pings, browser video, game audio —
# and all of it used to pulse the backdrop. PulseAudio (and pipewire-pulse)
# can instead record the monitor of one specific *sink input* via
# ``parec --monitor-stream=<index>``, so auto mode resolves tide's own
# playback stream and captures exactly that. mpv runs in-process with
# ``audio_client_name="tide"`` (player.py), so its stream is claimable by
# name; child-process backends (librespot) register their pid here and are
# claimed by ``application.process.id``. An explicit monitor picked in
# settings still wins over all of this.

_OWN_APP_NAMES = frozenset({"tide"})
_stream_pids: set[str] = set()
_stream_pids_lock = threading.Lock()


def register_stream_pid(pid: int | None) -> None:
    """Claim a child playback process's streams for auto capture.

    Called by backends that play through a separate process (librespot);
    their sink inputs carry the child's ``application.process.id``, not
    tide's, and would otherwise be invisible to auto resolution."""
    if pid:
        with _stream_pids_lock:
            _stream_pids.add(str(int(pid)))


def unregister_stream_pid(pid: int | None) -> None:
    if pid:
        with _stream_pids_lock:
            _stream_pids.discard(str(int(pid)))


def _own_pids() -> set[str]:
    with _stream_pids_lock:
        return {str(os.getpid())} | set(_stream_pids)


def _pick_own_sink_input(entries: list, pids: set[str]) -> int | None:
    """Pure matcher over ``pactl -f json list sink-inputs`` data: the index
    of tide's own playback stream, or None. Candidates are ranked:

      0. a stream whose pid we own or registered — proof of ownership
      1. a "tide"-named stream carrying no pid at all — mpv's in-process
         pipewire AO announces no application.process.id (seen live)
      2. a "tide"-named stream with somebody ELSE's pid — almost certainly
         a second tide process; last resort only, so two instances can't
         latch onto each other's audio

    Ranks 0 and 1 are BOTH this process's streams, so between them an
    uncorked (actually playing) stream wins outright — tide can briefly
    own two (a corked librespot idling while rank-1 mpv plays, or vice
    versa) and the corked one is the wrong tap: sorting rank above corked
    would latch the pulse/visualizer onto silence. Only the foreign-pid
    rank 2 is demoted below the others regardless of corked state."""
    own: list[tuple[tuple[bool, bool, int], int]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        props = entry.get("properties")
        if not isinstance(props, dict):
            continue
        pid = props.get("application.process.id")
        pid_known = pid is not None and str(pid).strip() != ""
        name_mine = (
            props.get("application.name") in _OWN_APP_NAMES
            or props.get("application.id") in _OWN_APP_NAMES
        )
        if pid_known and str(pid) in pids:
            rank = 0
        elif name_mine and not pid_known:
            rank = 1
        elif name_mine:
            rank = 2
        else:
            continue
        idx = entry.get("index")
        if isinstance(idx, int):
            own.append(
                ((rank == 2, bool(entry.get("corked", False)), rank), idx))
    if not own:
        return None
    return min(own, key=lambda item: item[0])[1]


def _own_sink_input() -> int | None:
    """Resolve tide's playback sink input right now, or None (pactl
    missing/failed, or tide simply has no open stream)."""
    if shutil.which("pactl") is None:
        return None
    try:
        out = subprocess.run(
            ["pactl", "-f", "json", "list", "sink-inputs"],
            capture_output=True, text=True, timeout=2,
        )
        entries = json.loads(out.stdout or "[]")
    except Exception:
        return None
    if not isinstance(entries, list):
        return None
    return _pick_own_sink_input(entries, _own_pids())


def _default_sink_monitor() -> str | None:
    """Return ``<default_sink>.monitor`` or None if pactl is missing."""
    if shutil.which("pactl") is None:
        return None
    try:
        out = subprocess.run(
            ["pactl", "info"], capture_output=True, text=True, timeout=2,
        )
    except Exception:
        return None
    sink = None
    for line in out.stdout.splitlines():
        if line.startswith("Default Sink:"):
            sink = line.split(":", 1)[1].strip()
            break
    if not sink:
        return None
    return sink + ".monitor"


def _capture_latency_from_info(info, pid: int, stream_idx: int) -> float | None:
    names = ("source_outputs", "sink_inputs", "sources", "sinks")
    if not isinstance(info, dict) or any(not isinstance(info.get(k), list) for k in names):
        return None

    def indexed(name, index):
        if type(index) is not int:
            return None
        matches = [row for row in info[name]
                   if isinstance(row, dict) and type(row.get("index")) is int
                   and row["index"] == index]
        return matches[0] if len(matches) == 1 else None

    captures = []
    for row in info["source_outputs"]:
        if not isinstance(row, dict):
            continue
        props = row.get("properties")
        if (isinstance(props, dict)
                and str(props.get("application.process.id")) == str(pid)
                and props.get("application.name") == "tide-visualizer"):
            captures.append(row)
    if len(captures) != 1:
        return None
    capture = captures[0]
    playback = indexed("sink_inputs", stream_idx)
    if playback is None or capture.get("corked") or playback.get("corked"):
        return None
    sink = indexed("sinks", playback.get("sink"))
    monitor = indexed("sources", capture.get("source"))
    if sink is None or monitor is None:
        return None
    # pactl names a source's owning sink "monitor_source" in JSON too.
    if (not isinstance(sink.get("name"), str) or not isinstance(monitor.get("name"), str)
            or monitor.get("monitor_source") != sink["name"]
            or sink.get("monitor_source") != monitor["name"]):
        return None
    values = (capture.get("buffer_latency_usec"), capture.get("source_latency_usec"),
              playback.get("sink_latency_usec"))
    if any(type(v) not in (float, int) or not 0.0 <= v <= 5_000_000
           or not math.isfinite(v) for v in values):
        return None
    # pa_timing_info subtracts sink latency for a monitor. the tap can lead
    # the speakers; clamping its signed contribution would erase that fact.
    # pactl omits client transport/pipe latency, so this is one component.
    return (values[0] + values[1] - values[2]) / 1_000_000.0


def _query_capture_latency(pid: int, stream_idx: int) -> float | None:
    if shutil.which("pactl") is None:
        return None
    try:
        result = subprocess.run(["pactl", "-f", "json", "list"],
                                capture_output=True, text=True, timeout=0.5)
        if result.returncode != 0 or len(result.stdout) > 4 * 1024 * 1024:
            return None
        return _capture_latency_from_info(json.loads(result.stdout), pid, stream_idx)
    except Exception:
        return None


class AudioVisualizerFeed(QObject):
    bands_updated = Signal(object)         # numpy.ndarray (BANDS,)
    waveform_updated = Signal(object)      # numpy.ndarray (CHUNK,)
    pulse_updated = Signal(float)          # bass-energy envelope, 0..1
    pulse_frame = Signal(object)           # immutable PulseFrame for timeline recording
    error = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._running = False
        self._monitor = "(not started)"
        # Capture scoping. explicit_source is a user-picked monitor (wins
        # outright); otherwise auto mode claims tide's own sink input
        # (stream_idx) and only falls back to the default sink's monitor
        # while tide has no open stream.
        self._explicit_source: str | None = None
        self._stream_idx: int | None = None
        self._next_upgrade: float = 0.0
        self._prev_bands: np.ndarray | None = None
        self._pulse_env: _PulseState | None = None
        # Opt-in per-chunk diagnostics CSV (TIDE_PULSE_TRACE) — see the
        # pulse-trace block above. Opened per capture session in start().
        self._tracer: _PulseTracer | None = None
        self._t_capture0: float = 0.0
        self._pulse_latency_override = _configured_pulse_latency()
        self._pulse_probe_proc: subprocess.Popen | None = None
        self._pulse_capture_latency: tuple[subprocess.Popen, float] | None = None
        # Reference-counted consumers. The singleton feed is shared by the
        # visualizer view and the app-wide ambient-pulse controller; capture
        # runs while at least one consumer holds it so neither tears it down
        # under the other.
        self._consumers: set[str] = set()
        self._preferred_source: str | None = None
        self._atexit_registered = False

    @property
    def running(self) -> bool:
        return self._running

    @property
    def device(self) -> str:
        return self._monitor

    # ---------- reference-counted lifecycle ----------

    def add_consumer(self, name: str, source: str | None = None) -> bool:
        """Register a consumer and ensure capture is running. Returns True if
        the feed is live afterwards. ``source`` sets the preferred monitor
        (used only when the feed has to be (re)started)."""
        self._consumers.add(name)
        if source is not None:
            self._preferred_source = source
        if not self._running:
            return self.start(source=self._preferred_source)
        return True

    def remove_consumer(self, name: str) -> None:
        """Drop a consumer; stop capture once nobody holds it."""
        self._consumers.discard(name)
        if not self._consumers and self._running:
            self.stop()

    def set_source(self, source: str | None) -> None:
        """Change the preferred monitor. Restarts an in-flight capture so the
        new device takes effect while keeping consumers registered."""
        self._preferred_source = source
        if self._running:
            holders = set(self._consumers)
            self.stop()
            self._consumers = holders
            self.start(source=self._preferred_source)

    def _resolve_target(self) -> tuple[str | None, int | None]:
        """(monitor_name, stream_index) for the next parec spawn. Stream
        mode when auto resolution claims one of tide's own sink inputs;
        monitor mode for an explicit user-chosen source, or as the auto
        fallback while tide has no open playback stream."""
        if self._explicit_source:
            return self._explicit_source, None
        idx = _own_sink_input()
        if idx is not None:
            return None, idx
        return _default_sink_monitor(), None

    def start(self, source: str | None = None) -> bool:
        if self._running:
            return True
        if shutil.which("parec") is None:
            self.error.emit("parec not found — install libpulse")
            return False
        self._explicit_source = source or None
        monitor, stream_idx = self._resolve_target()
        if monitor is None and stream_idx is None:
            self.error.emit("couldn't resolve a sink monitor — check pactl info")
            return False

        try:
            self._proc = self._spawn_parec(monitor, stream_idx)
        except Exception as exc:
            self.error.emit(f"parec failed to start: {exc}")
            return False

        self._stream_idx = stream_idx
        self._monitor = (
            f"tide (stream #{stream_idx})" if stream_idx is not None
            else str(monitor)
        )
        self._next_upgrade = time.monotonic() + 2.0
        trace_path = os.environ.get(_TRACE_ENV)
        if trace_path:
            try:
                self._tracer = _PulseTracer(trace_path)
            except Exception:
                self._tracer = None
        self._t_capture0 = time.monotonic()
        self._pulse_latency_override = _configured_pulse_latency()
        self._stop.clear()
        self._thread = threading.Thread(target=self._process_loop, name="tide-fft", daemon=True)
        self._thread.start()
        self._running = True
        # The worker emits Qt signals, so it must be joined before this
        # QObject's C++ half can be deleted — otherwise a still-running loop
        # raises "Signal source has been deleted" at interpreter teardown
        # (seen when a test suite starts capture and never stops it). The
        # bound method also pins this instance until exit, same retain-until-
        # joined idea as qthreads.py; stop() is idempotent and runs on the
        # main thread, where PySide teardown is safe.
        if not self._atexit_registered:
            atexit.register(self.stop)
            self._atexit_registered = True
        return True

    def stop(self) -> None:
        if not self._running:
            return
        self._stop.set()
        if self._proc is not None:
            try:
                self._proc.terminate()
            except Exception:
                pass
            try:
                self._proc.wait(timeout=1.0)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        self._thread = None
        self._pulse_probe_proc = None
        self._pulse_capture_latency = None
        self._running = False
        self._prev_bands = None
        self._pulse_env = None
        self._consumers.clear()
        if self._tracer is not None:
            self._tracer.close()
            self._tracer = None

    # ---------- worker ----------

    def _start_pulse_latency_probe(self, proc: subprocess.Popen) -> None:
        if (self._pulse_latency_override is not None or self._stream_idx is None
                or self._pulse_probe_proc is proc or type(getattr(proc, "pid", None)) is not int):
            return
        self._pulse_probe_proc = proc
        self._pulse_capture_latency = None
        # wait for the first audio chunk: the source output may not exist at
        # spawn time. a separate daemon keeps pactl out of the fft/gui paths.
        thread = threading.Thread(target=self._measure_pulse_latency,
                                  args=(proc, self._stream_idx),
                                  name="tide-pulse-latency", daemon=True)
        try:
            thread.start()
        except RuntimeError:
            pass

    def _measure_pulse_latency(self, proc: subprocess.Popen, stream_idx: int) -> None:
        # pactl can race the source output's appearance right after the
        # first chunk (and pipewire's enumeration has its own races); one
        # miss used to pin the 25 ms fallback for the whole session.
        for _attempt in range(_PULSE_PROBE_TRIES):
            if (self._stop.is_set() or self._proc is not proc
                    or self._stream_idx != stream_idx):
                return
            latency = _query_capture_latency(proc.pid, stream_idx)
            if latency is not None:
                if (not self._stop.is_set() and self._proc is proc
                        and self._stream_idx == stream_idx):
                    self._pulse_capture_latency = (proc, latency)
                return
            if self._stop.wait(_PULSE_PROBE_RETRY_S):
                return

    def _emit_pulse_frame(self, state: _PulseState | None,
                          started_at: float | None = None,
                          energy: float | None = None) -> None:
        now = time.monotonic()
        latency = self._pulse_latency_override
        if latency is None:
            elapsed = max(0.0, now - started_at) if started_at is not None else 0.0
            capture = _PULSE_CAPTURE_REQUEST_S
            measured = self._pulse_capture_latency
            if measured is not None and measured[0] is self._proc:
                capture = measured[1]
            latency = capture + _PULSE_ONSET_LATENCY_S + elapsed
        kind = _pulse_kind(state.diag) if state is not None else "sustain"
        if energy is not None and (not math.isfinite(energy) or energy < 0.0):
            energy = None
        self.pulse_frame.emit(PulseFrame(
            captured_at=now,
            level=float(state.env) if state is not None else 0.0,
            kind=kind,
            latency=latency,
            reset=state is None,
            scoped=self._stream_idx is not None,
            onset=_pulse_onset(state.diag, kind) if state is not None else 0.0,
            # a reset marks a gap; silence would be a zero-energy measurement.
            energy=energy if state is not None else None,
        ))

    def _trace_reset(self) -> None:
        """One reset marker row when the pulse baseline drops, so stream
        gaps are visible in a trace. Callers guard on the baseline having
        actually existed, keeping a paused stream from spamming markers."""
        self._emit_pulse_frame(None)
        if self._tracer is not None:
            self._tracer.row(
                time.monotonic() - self._t_capture0, None, reset=True)

    def _spawn_parec(self, monitor: str | None,
                     stream_idx: int | None = None) -> subprocess.Popen:
        argv = ["parec"]
        if stream_idx is not None:
            # Per-stream capture: the monitor of ONE sink input — tide's
            # own playback — rather than a whole sink shared with every
            # other app on the desktop.
            argv += ["--monitor-stream", str(stream_idx)]
        else:
            argv += ["-d", str(monitor)]
        argv += [
            "--rate", str(SAMPLE_RATE),
            "--channels", "1",
            "--format", "float32le",
            "--raw",
            "--latency-msec", "10",
            "--client-name", "tide-visualizer",
        ]
        return subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )

    def _process_loop(self) -> None:
        chunk_bytes = CHUNK * 4   # float32
        respawns = 0
        while not self._stop.is_set():
            proc = self._proc
            if proc is None or proc.stdout is None:
                return
            stdout = proc.stdout
            # With bufsize=0 the pipe is a raw FileIO: read() returns whatever
            # is currently buffered, and parec at --latency-msec 10 delivers
            # ~1.8KB every 10ms — well under a chunk. Short reads are the
            # normal case, so accumulate them here; discarding them (as this
            # loop once did) drops audio and adds 50-70ms of latency per
            # discarded stretch, which read as the background pulsing behind
            # the beat.
            pending = bytearray()
            died = False
            while not self._stop.is_set():
                # Auto mode parked on the whole-sink fallback: watch for
                # tide's own stream appearing (play pressed after capture
                # started) and jump to it. Ending this parec is the whole
                # trigger — the respawn path re-resolves and lands on the
                # stream. Time-gated so pactl runs at most every ~2 s, and
                # only reached while audio flows, which is exactly when a
                # wrong-scope capture matters.
                if (
                    self._explicit_source is None
                    and self._stream_idx is None
                    and time.monotonic() >= self._next_upgrade
                ):
                    self._next_upgrade = time.monotonic() + 2.0
                    if _own_sink_input() is not None:
                        try:
                            proc.terminate()
                            proc.wait(timeout=1.0)
                        except Exception:
                            pass
                        died = True
                        break
                try:
                    data = stdout.read(chunk_bytes)
                except Exception:
                    died = True
                    break
                if not data:
                    # EOF. Distinguish "stream paused" from "parec exited". A
                    # dead child returns EOF forever; without the poll() this
                    # loop spun on it for the rest of the session while the
                    # process sat unreaped in the table.
                    if proc.poll() is not None:
                        died = True
                        break
                    # Paused stream = a time gap for the onset detector too;
                    # keep the baseline dropped while no audio flows so the
                    # first post-resume chunk can't flux against pre-pause
                    # audio. Chunk processing is idle here, so this is free.
                    if self._pulse_env is not None:
                        self._trace_reset()
                    self._pulse_env = None
                    if self._stop.wait(0.05):
                        break
                    continue
                respawns = 0   # healthy data — reset the give-up counter
                pending += data
                if len(pending) < chunk_bytes:
                    continue
                # If the thread stalled (GUI hiccup, suspend), skip stale
                # audio rather than replaying it late.
                if len(pending) > chunk_bytes * 4:
                    del pending[:len(pending) - chunk_bytes * 2]
                    # The dropped stretch is a time gap. Differencing onset
                    # flux across it reads "quiet before the stall, loud
                    # after" as a kick that never happened — re-anchor the
                    # pulse baseline on the next chunk instead.
                    if self._pulse_env is not None:
                        self._trace_reset()
                    self._pulse_env = None
                while len(pending) >= chunk_bytes and not self._stop.is_set():
                    samples = np.frombuffer(
                        bytes(pending[:chunk_bytes]), dtype=np.float32
                    )
                    del pending[:chunk_bytes]
                    analysis_started = time.monotonic()
                    try:
                        self._prev_bands = _compute_bands(samples, self._prev_bands)
                    except Exception:
                        self._pulse_env = None
                        self._trace_reset()
                        continue
                    # Re-check right before emitting: stop() may have been
                    # called (possibly at teardown) since this chunk began,
                    # and emitting from a deleted signal source raises.
                    if self._stop.is_set():
                        return
                    self.bands_updated.emit(self._prev_bands.copy())
                    self.waveform_updated.emit(samples.copy())
                    try:
                        self._pulse_env = _compute_pulse(samples, self._pulse_env)
                    except Exception:
                        self._pulse_env = None
                    else:
                        # None = non-finite chunk dropped the baseline; skip
                        # the emit, the next clean chunk re-anchors.
                        if self._pulse_env is not None:
                            self.pulse_updated.emit(float(self._pulse_env.env))
                    energy = (_pulse_energy(samples)
                              if self._pulse_env is not None else None)
                    self._emit_pulse_frame(self._pulse_env, analysis_started, energy)
                    if self._tracer is not None:
                        state = self._pulse_env
                        self._tracer.row(
                            time.monotonic() - self._t_capture0,
                            state.diag if state is not None else None,
                            reset=state is None,
                        )
                    if self._pulse_env is not None:
                        self._start_pulse_latency_probe(proc)
            if not died or self._stop.is_set():
                return
            # parec exited underneath us (sink unplugged, pipewire restart).
            # Respawn a few times so an audio-server hiccup doesn't
            # permanently kill the visualizer/ambient pulse mid-session.
            respawns += 1
            if respawns > 3:
                self._running = False
                self.error.emit("audio capture stopped — parec keeps exiting")
                return
            if self._stop.wait(0.5):
                return
            # Re-resolve on every respawn: tide's stream index changes when
            # playback stops/starts or the backend switches (mpv ↔
            # librespot), and the fallback↔stream upgrade arrives here too.
            monitor, stream_idx = self._resolve_target()
            if monitor is None and stream_idx is None:
                monitor = _default_sink_monitor()
            if stream_idx != self._stream_idx:
                # The target moved — a re-resolution, not a crash loop.
                respawns = 0
            try:
                self._proc = self._spawn_parec(monitor, stream_idx)
            except Exception as exc:
                self._running = False
                self.error.emit(f"parec died and couldn't restart: {exc}")
                return
            self._stream_idx = stream_idx
            self._monitor = (
                f"tide (stream #{stream_idx})" if stream_idx is not None
                else str(monitor)
            )
            self._next_upgrade = time.monotonic() + 2.0
            # New stream, new baseline: the first post-respawn chunk must
            # not compute onset flux against audio from before the gap —
            # that manufactures a full-strength "kick" out of a sustained
            # bass bed resuming after a pipewire hiccup.
            if self._pulse_env is not None:
                self._trace_reset()
            self._pulse_env = None


# Singleton.
_instance: AudioVisualizerFeed | None = None


def feed() -> AudioVisualizerFeed:
    global _instance
    if _instance is None:
        _instance = AudioVisualizerFeed()
    return _instance
